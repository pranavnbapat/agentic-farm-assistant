# 01 — Current Architecture of `farm_assistant`

A code-level trace of how the existing system works. File references point to
`../farm_assistant/app/...`.

## 1. Stack

| Concern | Implementation |
|---|---|
| Web API + UI | FastAPI, SSE chat stream, vanilla-JS browser UI |
| Generation | vLLM (OpenAI-compatible), model `qwen3-30b-a3b-awq` (`clients/vllm_client.py`) |
| Retrieval | OpenSearch via a custom RAG endpoint `OS_RAG_API_PATH = /llm_retrieve` (`clients/opensearch_client.py`, `services/search_service.py`) |
| Auth / sessions / profiles / facts / attachments | external **Django** backend (`CHAT_BACKEND_URL`) |
| Vision | optional vLLM vision model (`generate_vision_once`) |
| Concurrency | a global `asyncio.Semaphore` set in `main.py` lifespan |

FastAPI itself is **stateless** per request; durable state lives in Django. The only
in-process state is uploaded-PDF/image data and the generation semaphore.

## 2. Entry point — the chat turn

The whole conversation experience runs through one endpoint:
`GET /ask/stream` (aliased as `/chatbot/api/chats/{session_id}/message/stream`) in
`routers/ask.py`, function `ask_stream` → inner `gen()`. It returns an
`EventSourceResponse` (SSE). The POST variants just forward into the same function.

## 3. Request flow (as implemented)

```text
ask_stream()  [routers/ask.py]
│
├─ 1. Decode JWT → user_uuid                     (_extract_user_uuid_from_token)
├─ 2. Resolve attachments for the user           (pdf_service / image_service)
├─ 3. Empty / oversized-input guards
├─ 4. Load conversation state                    (chat_history.load_chat_state + merge_messages)
│       → history_text, prior messages, last assistant question
│
├─ 5. PRE-FLIGHT PIPELINE  (several small LLM hops, run concurrently)
│   ├─ profile_future        : load profile + facts + memory notes  (user_profile_service)
│   ├─ _resolve_turn_context : rewrite elliptical follow-ups → standalone intent
│   │                          (skipped when _looks_standalone(user_q))
│   ├─ _normalize_query_for_retrieval : spelling/grammar cleanup for search
│   └─ _route_turn_mode      : choose ONE of 7 turn modes  (see §4)
│       gathered via asyncio.gather(...)
│
├─ 6. BRANCH on turn mode
│   ├─ clarification_only / off_topic / history_only /
│   │  conversation_only / assistant_capabilities / general_knowledge
│   │        → NO retrieval, emit a status event, build a mode-specific prompt
│   └─ normal
│        → RETRIEVAL FLOW  (see §5)
│
├─ 7. Attachments merged into contexts/sources    (PDF text, image vision summaries)
├─ 8. Compute grounding_state                      (euf_supported / attachment_supported / general_fallback / <mode>)
├─ 9. Build prompt for the chosen mode             (prompt_service.build_*_messages)
├─ 10. Token-budget guard                          (prompt_cap vs NUM_CTX)
├─ 11. Acquire generation semaphore (queue if full)
├─ 12. STREAM tokens from vLLM over SSE            (stream_generate)
├─ 13. Citation post-processing                    (_extract_cited_numbers, _strip_orphan_citations)
│        → emit "sources" with only the cited subset
├─ 14. Emit timing, then "done"
│
└─ 15. POST-TURN (fire-and-forget asyncio.create_task)
        ├─ _maybe_update_session_title (first turn only)
        └─ UserProfileService.process_conversation_turn  (profile/fact extraction)
```

### SSE event vocabulary
`status` → `grounding` → `token`* → `stats` → `sources` → `timing` → `done`
(plus `app_error`). The browser renders tokens progressively, then shows citations.
**This `status` channel is important** — it is exactly the hook an agent loop would use
to narrate its steps.

## 4. Turn routing — the existing "soft router"

`_route_turn_mode()` is a **three-stage classifier**:

1. **Hard deterministic guardrails** (`_hard_route_turn_mode`): empty/punctuation →
   `clarification_only`; consumer-tech / general-trivia / prompt-injection terms →
   `off_topic`; file mentions bypass. These run *before* the LLM so repeated off-topic
   prompts can't drift into an answerable mode.
2. **Lightweight LLM classifier** (`_decide_turn_strategy`): a `generate_once` call
   (temperature 0, `max_tokens=14`, **1.5 s timeout**) returns JSON `{"mode": ...}`.
3. **Default**: `normal` on any failure/timeout.

The seven modes (`TurnMode`):

| Mode | Retrieval? | Prompt builder |
|---|---|---|
| `clarification_only` | no | `build_clarification_messages` |
| `off_topic` | no | `build_off_topic_messages` |
| `history_only` | no | `build_history_only_messages` |
| `conversation_only` | no | `build_conversation_only_messages` |
| `assistant_capabilities` | no | `build_capabilities_messages` |
| `general_knowledge` | no | `build_general_knowledge_messages` |
| `normal` | **yes** | `build_messages` |

This is genuinely a routing layer — it decides *whether to retrieve* — which is one of the
pillars of agentic RAG. The limitation is that it is a **one-shot classification**, not a
revisable decision.

## 5. Retrieval flow (`normal` mode only)

```text
build_search_payload(inp)                         [search_service.py]
   → POST {search_term, k=max(top_k, RETRIEVAL_CANDIDATE_K=10), model:"msmarco", ...}
     to OpenSearch /llm_retrieve
   → collect_os_items (dedupe by _id across pages)
│
filter_items_by_min_score(items, RETRIEVAL_MIN_SCORE=1.0)   [context_service.py]
   → drop hits below the score floor
│
build_context_and_sources(items, question, top_k, MAX_CONTEXT_CHARS=24000)
   → split llm_context / ko_content_flat into paragraphs
   → rank_paragraphs (token overlap + keyword/topic boost + front-load)
   → collapse multiple chunks per parent_id into one citation (PER_PARENT_CHAR_CAP=3500)
   → emit contexts[] + SourceItem[]   (kept positionally in lockstep)
│
estimate_retrieval_quality(question, items, top_n=3)        [context_service.py]
   → token-overlap ratio of query vs title/desc of top items
   → IF quality < 0.15:  DROP all contexts/sources  → treat as "no sources found"
```

So there are **three independent quality gates** before generation:
a hard score floor, a per-parent budget, and a semantic-overlap relevance gate.

### Grounding state
After retrieval the code labels the turn:

- `euf_supported` — usable EU-FarmBook contexts survived
- `attachment_supported` — only uploaded PDF/image content grounds the answer
- `general_fallback` — nothing survived → the prompt instructs an honest "no source
  material found, here is a cautious best-effort answer" (see `build_messages`,
  `has_relevant_sources=False`)

**This is the critical behaviour to notice:** when retrieval fails the quality gate, the
system does **not** retry. It downgrades to a disclaimed general-knowledge answer. An
agentic system would instead *act* on that signal.

## 6. Prompt assembly (`prompt_service.py`)

Each mode composes a system prompt from reusable directive blocks (`_IDENTITY`,
`_SCOPE_RULE`, `_LANGUAGE_RULE`, `_BREVITY_RULE`, `_FORMATTING_RULE`, `_FOLLOWUP_RULE`),
then `_assemble_messages` produces an OpenAI-style `[system, ...history, user]` array.
For `normal` turns, `_attach_sources` prepends a numbered `[1]..[N]` context block to the
user turn so citations stay anchored. Profile context is injected as "latent background"
(and deliberately withheld on casual/off-topic/capability turns).

## 7. Generation & citations

`stream_generate` (`vllm_client.py`) posts to `/v1/chat/completions` with `stream:true`
and yields token deltas. After the stream, `ask.py` normalises citation forms
(`[S1]`, `(source: 1)`, ranges) to `[N]`, strips any citation number not in the real
source set (`_strip_orphan_citations`), and emits only the **cited** sources.

## 8. Personalization loop (asynchronous)

`UserProfileService.process_conversation_turn` runs after the answer (fire-and-forget):
multilingual analysis → LLM/keyword extraction → dedup → writes profile fields, facts, and
memory notes back to Django. On the next turn, `build_profile_context` re-injects a compact
summary. This is a slow, cross-turn personalization loop — not part of the answer path.

## 9. What the architecture optimises for

- **Latency**: aggressive timeouts on the pre-flight hops (1.2–2.0 s), skip-when-confident
  shortcuts (`_looks_standalone`), and `asyncio.gather` concurrency.
- **Responsiveness**: SSE token streaming + `status` events.
- **Safety/scope**: deterministic off-topic guardrails before any LLM call.
- **Citation integrity**: orphan-citation stripping.

These are real strengths that an agentic redesign **must preserve** — see
[03-target-architecture.md](./03-target-architecture.md).
