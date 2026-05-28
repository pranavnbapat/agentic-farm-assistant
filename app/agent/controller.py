# app/agent/controller.py
#
# The agentic RAG controller. Turns farm_assistant's linear pipeline into a bounded
# loop:  route -> (plan) -> [ search -> grade -> correct ]* -> synthesize.
#
# It is an async generator that yields SSE-ready event dicts {"event","data"} so the
# router can stream them unchanged. Every decision point emits a `status` (for the UI)
# and a `trace` (for transparency / evaluation).
#
# What is reused from farm_assistant (via tools/services):
#   - routing.route_turn_mode .............. entry policy (7 modes, deterministic gate)
#   - query_prep.resolve_turn_context ...... follow-up resolution
#   - query_prep.normalize_query_for_retrieval
#   - tools.retrieve.retrieve .............. search + min-score filter + context build
#   - tools.grade.grade_relevance .......... heuristic (+optional LLM) relevance grade
#   - tools.synthesize ..................... mode -> prompt builder -> token stream
#   - utils.citations ...................... citation hygiene
# What is NEW:
#   - this loop, query_prep.rewrite_query, query_prep.decompose_question

import asyncio
import logging
import time
from typing import AsyncGenerator, Optional

import httpx

from app.config import get_settings
from app.agent.policies import AgentPolicies
from app.agent.state import AgentState, TraceStep
from app.tools import routing
from app.tools.query_prep import (
    resolve_turn_context,
    normalize_query_for_retrieval,
    should_skip_query_normalization,
    rewrite_query,
    decompose_question,
)
from app.tools.retrieve import retrieve
from app.tools.grade import grade_relevance, verify_constraints, verify_answer_grounding
from app.tools.synthesize import build_answer_messages, stream_answer
from app.utils.citations import (
    sanitize_generated_markdown,
    finalize_citations,
    sources_to_payload,
)
from app.utils.history import normalize_history, format_history, last_assistant_question
from app.utils.language_utils import detect_language_confident, get_language_name
from app.services.chat_history import (
    load_chat_state,
    merge_messages,
    log_turn_to_backend,
    extract_user_uuid_from_token,
)
from app.services.user_profile_service import UserProfileService

logger = logging.getLogger("agentic-fa.controller")
S = get_settings()

# Status banner per non-retrieval mode (mirrors farm_assistant's UX strings).
_DIRECT_MODE_STATUS = {
    "clarification_only": ("Clarify", "Need a clearer question..."),
    "off_topic": ("Scope", "Off-topic question, skipping search..."),
    "history_only": ("History", "Answering from conversation history..."),
    "conversation_only": ("Conversation", "Answering from the current conversation..."),
    "assistant_capabilities": ("Capabilities", "Answering about assistant capabilities..."),
    "general_knowledge": ("Knowledge", "Answering from general agricultural knowledge..."),
}


def _ev(event: str, data) -> dict:
    return {"event": event, "data": data}


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


async def _load_profile_context(user_uuid: str, auth_token: str) -> str:
    """Fetch the user's profile + facts + memory notes and build latent background context."""
    try:
        profile = await UserProfileService.get_or_create_profile(user_uuid, auth_token)
        facts, memory_notes = await asyncio.gather(
            UserProfileService.get_facts(user_uuid, auth_token, limit=5),
            UserProfileService.get_memory_notes(user_uuid, auth_token, limit=10),
        )
        return UserProfileService.build_profile_context(profile, facts, memory_notes)
    except Exception as e:
        logger.warning(f"Failed to load user profile: {e}")
        return ""


async def run_turn(
    *,
    user_q: str,
    history_messages: Optional[list[dict]] = None,
    auth_token: Optional[str] = None,
    session_id: Optional[str] = None,
    profile_context: Optional[str] = None,
    pause_personalization: bool = False,
    top_k: Optional[int] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    model: Optional[str] = None,
    followup_hint: str = "",
    semaphore: Optional[asyncio.Semaphore] = None,
    policies: Optional[AgentPolicies] = None,
) -> AsyncGenerator[dict, None]:
    policies = policies or AgentPolicies.from_settings()
    t0 = time.perf_counter()

    state = AgentState(user_q=(user_q or "").strip())
    state.profile_context = profile_context

    if not state.user_q:
        yield _ev("app_error", {"message": "Empty question"})
        return
    if _estimate_tokens(state.user_q) > S.MAX_USER_INPUT_TOKENS:
        yield _ev("app_error", {
            "message": f"Question is too long. Limit is ~{S.MAX_USER_INPUT_TOKENS} tokens per message."
        })
        return

    # Conversation history: merge persisted (Django) history with client-supplied turns.
    client_messages = [m for m in (history_messages or []) if isinstance(m, dict)]
    if session_id and auth_token:
        backend_state = await load_chat_state(session_id, auth_token)
        merged_history = merge_messages(backend_state.get("messages", []), client_messages)
    else:
        merged_history = client_messages
    state.history_messages = normalize_history(merged_history)
    state.history_text = format_history(merged_history)

    # Personalization (latent background): load the user's profile/facts/memory in the
    # background so it overlaps routing + retrieval. It is injected only as background and
    # only on substantive turns — the live conversation history stays the primary context.
    user_uuid = extract_user_uuid_from_token(auth_token)
    profile_task = None
    if profile_context is None and user_uuid and auth_token and not pause_personalization:
        profile_task = asyncio.create_task(_load_profile_context(user_uuid, auth_token))

    t_k = top_k if isinstance(top_k, int) and top_k > 0 else S.TOP_K
    _max_tokens = max_tokens if isinstance(max_tokens, int) and 0 < max_tokens <= S.MAX_OUTPUT_TOKENS else S.MAX_OUTPUT_TOKENS
    _temperature = temperature if isinstance(temperature, (int, float)) else S.TEMPERATURE
    _model = model or S.VLLM_MODEL

    # ----------------------------------------------------------------- #
    # 1. PLAN: resolve the turn into a standalone intent, then route.
    # ----------------------------------------------------------------- #
    yield _ev("status", {"stage": "Routing", "message": "Understanding the question..."})

    if routing.looks_standalone(state.user_q):
        state.effective_q = state.user_q
        state.prompt_q = state.user_q
    else:
        tc = await resolve_turn_context(
            question=state.user_q,
            history_text=state.history_text,
            last_assistant_question=last_assistant_question(history_messages),
            followup_hint=followup_hint or "",
        )
        state.effective_q = (tc.get("resolved_user_message") or state.user_q).strip() or state.user_q
        state.prompt_q = (tc.get("assistant_instruction") or state.effective_q).strip() or state.effective_q

    state.mode = await routing.route_turn_mode(
        user_q=state.user_q,
        prompt_q=state.prompt_q,
        history_text=state.history_text,
    )
    step = state.add_trace(TraceStep("route", detail=f"mode={state.mode}", query=state.prompt_q))
    yield _ev("trace", step.to_dict())

    # ----------------------------------------------------------------- #
    # 2. ACT: retrieval / CRAG loop (only for `normal`), else direct answer.
    # ----------------------------------------------------------------- #
    if state.mode in routing.RETRIEVAL_MODES:
        # 2a. spelling/grammar normalisation for search
        if should_skip_query_normalization(state.effective_q):
            state.retrieval_q = state.effective_q
        else:
            state.retrieval_q = await normalize_query_for_retrieval(state.effective_q)

        # 2b. optional decomposition (planner)
        queries = [state.retrieval_q]
        if policies.enable_decomposition:
            parts = await decompose_question(state.effective_q, policies.max_subqueries)
            if len(parts) > 1:
                queries = parts
                yield _ev("status", {"stage": "Planning", "message": "Breaking the question into parts..."})
                step = state.add_trace(TraceStep("plan", detail=f"{len(parts)} sub-queries", query=" | ".join(parts)))
                yield _ev("trace", step.to_dict())

        # 2c. bounded retrieve -> grade -> correct loop
        best_result = None
        best_grade = None
        attempt = 0
        queries_cur = list(queries)

        while True:
            yield _ev("status", {
                "stage": "Search",
                "message": "Searching EU-FarmBook sources..." if attempt == 0
                else "Searching again with a refined query...",
            })
            try:
                result = await retrieve(
                    queries_cur,
                    rank_question=state.effective_q,
                    top_k=t_k,
                    max_context_chars=S.MAX_CONTEXT_CHARS,
                    min_score=S.RETRIEVAL_MIN_SCORE,
                )
            except httpx.HTTPStatusError as e:
                body = (e.response.text or "")[:300]
                yield _ev("app_error", {"stage": "search", "status": e.response.status_code, "body": body})
                return
            except httpx.HTTPError as e:
                yield _ev("app_error", {"stage": "search", "message": f"Retrieval failed: {e}"})
                return

            step = state.add_trace(TraceStep(
                "search",
                detail=f"{result.raw_count} hits -> {len(result.contexts)} contexts",
                query=" | ".join(queries_cur),
            ))
            yield _ev("trace", step.to_dict())

            grade = await grade_relevance(
                state.effective_q,
                result.items,
                result.contexts,
                good_threshold=policies.grade_good,
                bad_threshold=policies.grade_bad,
                enable_llm_grader=policies.enable_llm_grader,
                llm_pass=policies.llm_grade_pass,
            )
            yield _ev("status", {
                "stage": "Assess",
                "message": f"Checking relevance ({grade.verdict}, {grade.score:.2f})...",
            })
            step = state.add_trace(TraceStep(
                "grade", detail=f"{grade.method}: {grade.detail}",
                grade_score=grade.score, verdict=grade.verdict,
            ))
            yield _ev("trace", step.to_dict())

            if best_grade is None or grade.score > best_grade.score:
                best_result, best_grade = result, grade

            state.iterations = attempt

            if grade.verdict == "good":
                break
            if attempt >= policies.max_corrections:
                break
            # Corrective rewrite only applies to the single-query path; for a
            # decomposed plan we accept the best pooled result instead of looping.
            if len(queries_cur) != 1:
                break

            attempt += 1
            new_q = await rewrite_query(queries_cur[0], reason=grade.detail)
            step = state.add_trace(TraceStep("rewrite", detail=f"reason: {grade.detail}", query=new_q))
            yield _ev("status", {"stage": "Refine", "message": "Refining the search and trying again..."})
            yield _ev("trace", step.to_dict())
            queries_cur = [new_q]

        # 2d. decide final grounding
        if best_grade is not None and best_grade.verdict == "good":
            state.contexts = best_result.contexts
            state.sources = best_result.sources
        elif best_result is not None and not policies.drop_weak_contexts:
            state.contexts = best_result.contexts
            state.sources = best_result.sources
        else:
            # Exhausted corrections and still weak: don't ground in noise.
            state.contexts = []
            state.sources = []

        # 2e. constraint verification — drop sources that match the topic but miss the
        # query's specific constraints (country/region, time, crop, species, project).
        pre_verify_n = len(state.sources)
        if state.contexts and policies.enable_constraint_filter:
            yield _ev("status", {"stage": "Verify", "message": "Checking sources match the question..."})
            keep = await verify_constraints(state.effective_q, state.sources)
            if keep is not None and len(keep) < len(state.sources):
                dropped = len(state.sources) - len(keep)
                state.contexts = [state.contexts[i] for i in keep if 0 <= i < len(state.contexts)]
                state.sources = [state.sources[i] for i in keep if 0 <= i < len(state.sources)]
                step = state.add_trace(TraceStep(
                    "verify",
                    detail=f"dropped {dropped} off-constraint source(s); kept {len(state.sources)}",
                ))
                yield _ev("trace", step.to_dict())

        # 2f. constraint recovery — if verification emptied the sources (topic matched but
        # the specific constraint did not), try ONE constraint-targeted re-retrieval before
        # falling back to a general answer.
        if policies.enable_constraint_recovery and pre_verify_n > 0 and not state.contexts:
            yield _ev("status", {"stage": "Recover", "message": "No on-constraint sources; retrying with a sharper query..."})
            recover_q = await rewrite_query(
                state.retrieval_q or state.effective_q,
                reason="results matched the topic but not the specific constraint "
                       "(country/region, time, crop, species, or named project); emphasize the constraint terms",
            )
            step = state.add_trace(TraceStep("recover", detail="constraint-targeted retry", query=recover_q))
            yield _ev("trace", step.to_dict())
            try:
                rec = await retrieve(
                    [recover_q], rank_question=state.effective_q, top_k=t_k,
                    max_context_chars=S.MAX_CONTEXT_CHARS, min_score=S.RETRIEVAL_MIN_SCORE,
                )
            except httpx.HTTPError:
                rec = None
            if rec and rec.contexts:
                rg = await grade_relevance(
                    state.effective_q, rec.items, rec.contexts,
                    good_threshold=policies.grade_good, bad_threshold=policies.grade_bad,
                    enable_llm_grader=False, llm_pass=policies.llm_grade_pass,
                )
                ctxs, srcs = rec.contexts, rec.sources
                if policies.enable_constraint_filter:
                    keep2 = await verify_constraints(state.effective_q, srcs)
                    if keep2 is not None:
                        ctxs = [ctxs[i] for i in keep2 if 0 <= i < len(ctxs)]
                        srcs = [srcs[i] for i in keep2 if 0 <= i < len(srcs)]
                if rg.verdict == "good" and ctxs:
                    state.contexts, state.sources = ctxs, srcs
                    state.iterations += 1
                    step = state.add_trace(TraceStep(
                        "recover", detail=f"recovered {len(srcs)} on-constraint source(s)",
                        verdict=rg.verdict, grade_score=rg.score,
                    ))
                else:
                    step = state.add_trace(TraceStep("recover", detail="still no on-constraint sources; honest fallback"))
                yield _ev("trace", step.to_dict())
            else:
                step = state.add_trace(TraceStep("recover", detail="recovery retrieval returned nothing"))
                yield _ev("trace", step.to_dict())

        # Grounding label: grounded if EUF sources survived grading; otherwise fall
        # back — to a clean general-knowledge answer when the router said
        # general_knowledge, or to the "no EUF material found" note for a normal turn.
        if state.contexts:
            state.grounding_state = "euf_supported"
        elif state.mode == "general_knowledge":
            state.grounding_state = "general_knowledge"
        else:
            state.grounding_state = "general_fallback"
    else:
        stage, message = _DIRECT_MODE_STATUS.get(state.mode, ("Answer", "Answering..."))
        yield _ev("status", {"stage": stage, "message": message})
        state.grounding_state = state.mode

    # ----------------------------------------------------------------- #
    # 3. SYNTHESIZE
    # ----------------------------------------------------------------- #
    if profile_task is not None:
        try:
            state.profile_context = (await profile_task) or None
        except Exception:
            state.profile_context = None

    question_for_prompt = state.user_q if state.mode == "clarification_only" else state.prompt_q
    has_relevant = bool(state.contexts)
    profile_for_prompt = None if state.mode == "clarification_only" else state.profile_context

    # Answer in the language of the user's question, not the (often non-English)
    # retrieved sources packed into the grounded turn. Only force a language when the
    # detector has positive evidence (a marker match); when it finds nothing it returns
    # None and we fall back to the reworded question-anchored rule.
    _q_lang_code = detect_language_confident(state.user_q)
    answer_language = get_language_name(_q_lang_code) if _q_lang_code else None
    if answer_language == "Unknown":
        answer_language = None

    # When EUF sources survived grading, synthesize a grounded, cited answer regardless
    # of whether the router originally guessed "normal" or "general_knowledge".
    synth_mode = "normal" if state.contexts else state.mode

    prompt_cap = min(S.MAX_INPUT_TOKENS, max(256, int(S.NUM_CTX) - int(_max_tokens) - 256))

    def _count(msgs) -> int:
        return sum(_estimate_tokens(m.get("content", "")) for m in msgs)

    def _build(hist, ctxs):
        return build_answer_messages(
            synth_mode,
            question=question_for_prompt,
            contexts=ctxs,
            history_messages=hist,
            profile_context=profile_for_prompt,
            has_relevant_sources=bool(ctxs),
            answer_language=answer_language,
        )

    # Conversation history grows unbounded across a long chat; combined with the retrieved
    # contexts it can exceed the model's input cap. Instead of hard-failing, keep a recency
    # window: drop the oldest history turns first, then (only if still over) trailing
    # context chunks — dropping the matching source so citations stay aligned.
    hist = list(state.history_messages)
    ctxs = list(state.contexts)
    srcs = list(state.sources)
    messages = _build(hist, ctxs)
    dropped_hist = dropped_ctx = 0
    while _count(messages) > prompt_cap:
        if hist:
            hist = hist[1:]
            dropped_hist += 1
        elif len(ctxs) > 1:
            ctxs = ctxs[:-1]
            srcs = srcs[:-1]
            dropped_ctx += 1
        else:
            break
        messages = _build(hist, ctxs)

    if dropped_hist or dropped_ctx:
        state.history_messages = hist
        state.contexts = ctxs
        state.sources = srcs
        has_relevant = bool(ctxs)
        if state.grounding_state == "euf_supported" and not ctxs:
            state.grounding_state = "general_fallback"
        step = state.add_trace(TraceStep(
            "trim",
            detail=f"trimmed to fit input budget ({prompt_cap} tok): dropped {dropped_hist} old "
                   f"history msg(s), {dropped_ctx} context(s)",
        ))
        yield _ev("trace", step.to_dict())

    prompt_tokens = _count(messages)
    if prompt_tokens > prompt_cap:
        # Even the minimal prompt (system + this question alone) exceeds the cap.
        yield _ev("app_error", {
            "message": f"Question is too long (~{prompt_tokens} tokens). Please shorten it."
        })
        return

    all_sources = sources_to_payload(state.sources)

    # Concurrency gate (shared with other in-flight generations).
    if semaphore is not None:
        if getattr(semaphore, "_value", 1) == 0:
            yield _ev("status", {"stage": "Queue", "message": "Waiting for a free slot..."})
        await semaphore.acquire()

    yield _ev("status", {"stage": "LLM", "message": "Composing the answer..."})

    t_llm = time.perf_counter()
    answer_chunks: list[str] = []
    try:
        async for chunk in stream_answer(
            messages, temperature=_temperature, max_tokens=_max_tokens, model=_model
        ):
            answer_chunks.append(chunk)
            yield _ev("token", chunk)
    except httpx.ConnectError as e:
        yield _ev("app_error", {"stage": "LLM", "message": f"Cannot connect to LLM: {e}"})
        return
    except httpx.HTTPStatusError as e:
        yield _ev("app_error", {"stage": "LLM", "status": e.response.status_code})
        return
    except Exception as e:
        logger.error(f"Unexpected LLM error: {e}")
        yield _ev("app_error", {"stage": "LLM", "message": f"Error: {e}"})
        return
    finally:
        if semaphore is not None:
            try:
                semaphore.release()
            except Exception:
                pass

    full_text = sanitize_generated_markdown("".join(answer_chunks))

    # ----------------------------------------------------------------- #
    # 4. CITATIONS + telemetry
    # ----------------------------------------------------------------- #
    cited_sources = finalize_citations(full_text, all_sources)

    # Honest grounding: only call it EUF-supported if the answer actually cited a source.
    # If EUF context was retrieved but the model cited nothing (it answered from general
    # knowledge instead), label it general_fallback rather than implying EUF backing.
    if cited_sources:
        state.grounding_state = "euf_supported"
    elif state.grounding_state == "euf_supported":
        state.grounding_state = "general_fallback"

    # Self-RAG-lite: verify the grounded answer against its cited sources. If it goes beyond
    # what the sources support, append (and stream) an honest caveat so the answer never
    # overstates the EU-FarmBook backing.
    if cited_sources and policies.enable_answer_verification:
        yield _ev("status", {"stage": "Verify-answer", "message": "Checking the answer against its sources..."})
        verdict, vnote = await verify_answer_grounding(state.prompt_q, full_text, state.sources)
        if verdict in ("supported", "partial", "unsupported"):
            yield _ev("verification", {"verdict": verdict, "note": vnote})
            step = state.add_trace(TraceStep("verify_answer", detail=f"{verdict}: {vnote}"[:200]))
            yield _ev("trace", step.to_dict())
            if verdict in ("partial", "unsupported"):
                caveat = (
                    "\n\n_Note: parts of this answer draw on general agricultural knowledge "
                    "beyond the cited EU-FarmBook sources._"
                )
                full_text += caveat
                yield _ev("token", caveat)

    yield _ev("grounding", {"mode": state.grounding_state})
    yield _ev("sources", cited_sources)

    yield _ev("timing", {
        "total_ms": int((time.perf_counter() - t0) * 1000),
        "llm_ms": int((time.perf_counter() - t_llm) * 1000),
        "iterations": state.iterations,
        "mode": state.mode,
        "grounding": state.grounding_state,
    })
    step = state.add_trace(TraceStep("synthesize", detail=f"grounding={state.grounding_state}, sources_cited={len(cited_sources)}"))
    yield _ev("trace", step.to_dict())

    # Persist the completed turn to Django — same chat_session / chat_message tables
    # farm_assistant writes. `meta` keys mirror what euf log_chat_turn parses
    # (model / grounding_mode / latency_ms / sources / assistant.*).
    if auth_token and S.AUTO_PERSIST_TURNS and full_text.strip():
        meta = {
            "model": _model,
            "grounding_mode": state.grounding_state,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "sources": cited_sources,
            "assistant": {"agent_mode": state.mode, "agent_iterations": state.iterations},
        }
        try:
            new_sid = await log_turn_to_backend(
                session_id=session_id,
                user_message=state.user_q,
                assistant_message=full_text,
                meta=meta,
                auth_token=auth_token,
            )
        except Exception as e:
            logger.warning(f"Turn persistence failed: {e}")
            new_sid = session_id
        if new_sid:
            yield _ev("session", {"session_id": new_sid})

    # Learn from the turn (fire-and-forget): extract/store profile facts + memory so the
    # assistant grows more personalized across the ongoing conversation and future sessions.
    # Skipped on non-substantive turns (greetings, refusals, capability/clarification) and
    # when the user paused personalization — those carry no profile signal.
    if (
        user_uuid
        and auth_token
        and not pause_personalization
        and full_text.strip()
        and state.mode not in ("clarification_only", "off_topic", "conversation_only", "assistant_capabilities")
    ):
        asyncio.create_task(
            UserProfileService.process_conversation_turn(
                user_uuid, session_id, state.user_q, full_text, auth_token
            )
        )

    yield _ev("done", {"message": "complete"})
