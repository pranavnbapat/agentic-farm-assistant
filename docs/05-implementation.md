# 05 — Implementation (what was built)

This documents the code that now lives in `agentic_farm_assistant/app/`. It implements
**Phase 0–2** of [04-migration-roadmap.md](./04-migration-roadmap.md): tool adapters, the
bounded CRAG controller, and entry-routing + streaming-status integration.

> **`farm_assistant` is never touched or imported.** The pieces it shares (config,
> clients, search/context/prompt services) were **copied** into this package so the app
> is standalone. The agentic layer (`agent/`, `tools/`) is new.

## File tree

```text
agentic_farm_assistant/
├── docs/                       # this analysis + design + implementation notes
├── requirements.txt            # lean: fastapi, httpx, pydantic(-settings), sse-starlette, uvicorn
├── .env.sample                 # OpenSearch + vLLM + AGENT_* knobs
├── run.sh                      # uvicorn app.main:app on :18002
└── app/
    ├── main.py                 # FastAPI app, lifespan semaphore, /health, landing page
    ├── config.py               # Settings (+ new AGENT_* section)            [adapted]
    ├── schemas.py              # AskIn / SourceItem / ChatMessageStreamIn      [trimmed]
    ├── clients/
    │   ├── opensearch_client.py                                               [copied]
    │   └── vllm_client.py      # generate_once + stream_generate              [adapted]
    ├── services/
    │   ├── search_service.py   # OpenSearch /llm_retrieve orchestration       [copied]
    │   ├── context_service.py  # ranking, parent-collapse, quality estimate   [copied]
    │   └── prompt_service.py   # all per-mode prompt builders                 [copied]
    ├── tools/                  # ── NEW: thin adapters the agent calls ──
    │   ├── routing.py          # entry policy: 7-mode router + guardrails      [ported]
    │   ├── query_prep.py       # resolve/normalize [ported] + rewrite/decompose [new]
    │   ├── retrieve.py         # search_eufarmbook -> RetrievalResult
    │   ├── grade.py            # grade_relevance -> GradeResult (heuristic+LLM)
    │   └── synthesize.py       # mode -> prompt builder -> token stream
    ├── agent/                  # ── NEW: the controller ──
    │   ├── state.py            # AgentState + TraceStep
    │   ├── policies.py         # AgentPolicies (loop knobs from config)
    │   └── controller.py       # run_turn(): the bounded CRAG loop
    ├── routers/
    │   └── ask.py              # SSE endpoint; relays controller events
    └── utils/
        ├── citations.py        # citation normalize/strip/finalize            [ported]
        └── history.py          # client-history formatting helpers
```

## The control flow (`agent/controller.py::run_turn`)

`run_turn` is an async generator yielding SSE-ready `{"event","data"}` dicts:

```text
1. PLAN     resolve follow-ups (resolve_turn_context, skipped if looks_standalone)
            route_turn_mode -> one of 7 modes        [emit status:Routing, trace:route]

2. BRANCH
   ├─ non-retrieval mode (off_topic / conversation_only / history_only /
   │  assistant_capabilities / general_knowledge / clarification_only)
   │      -> emit a mode status, answer directly (NO search)
   │
   └─ normal | general_knowledge -> CRAG LOOP:
        normalize_query_for_retrieval
        (optional) decompose_question -> sub-queries          [emit status:Planning]
        loop (bounded by AGENT_MAX_CORRECTIONS):
            retrieve(queries)                                 [emit status:Search, trace]
            grade_relevance(...)                              [emit status:Assess, trace]
            if good            -> break
            if no budget left  -> break
            rewrite_query(reason) -> retry                    [emit status:Refine, trace]
        keep the best attempt; if still weak and DROP_WEAK_CONTEXTS -> drop (general_fallback)
        verify_constraints(...) -> drop topical-but-off-constraint sources
                                                              [emit status:Verify, trace]

3. SYNTHESIZE
        build_answer_messages(mode, ...) (reuses prompt_service)
        token-budget guard; acquire generation semaphore
        stream tokens                          [emit status:LLM, grounding, token*]
        finalize_citations -> emit only cited sources         [emit sources]
        emit timing (incl. iterations) + done
```

## SSE event protocol

Identical to `farm_assistant` **plus** one new event:

| event | payload | notes |
|---|---|---|
| `status` | `{stage, message}` | per agent step (Routing/Search/Assess/Refine/LLM/…) |
| `trace` | `{step, detail, query?, grade_score?, verdict?}` | **new** — agent-step transparency / eval |
| `grounding` | `{mode}` | `euf_supported` \| `general_fallback` \| `<turn-mode>` |
| `token` | `"<text>"` | raw streamed chunk |
| `sources` | `[{n, sid, id, title, project, url, …}]` | cited subset only |
| `timing` | `{total_ms, llm_ms, iterations, mode, grounding}` | `iterations` is new |
| `verification` | `{verdict, note}` | **new** — Self-RAG check of the grounded answer vs its sources (`supported`/`partial`/`unsupported`) |
| `session` | `{session_id}` | **new** — the session_uuid the turn was persisted to (Django may auto-create one) |
| `done` / `app_error` | `{...}` | |

The existing UI works unchanged; `trace` and `session` are additive.

## Behaviour note — `general_knowledge` still probes EU-FarmBook

The LLM router frequently labels EUF-covered agricultural questions as
`general_knowledge` (e.g. "use of drones in sheep monitoring"). Because that mode used to
skip retrieval, such answers came back ungrounded with no citations. Fix: both `normal`
**and** `general_knowledge` go through the retrieve→grade→correct loop
(`routing.RETRIEVAL_MODES`). If EUF sources survive grading, the answer is synthesised
grounded with citations (`grounding=euf_supported`) regardless of the router's guess; only
when EUF genuinely has nothing relevant does it fall back — a clean general-knowledge answer
(`grounding=general_knowledge`) for a GK turn, or the "no EUF material found" note
(`grounding=general_fallback`) for a normal turn. Net effect: the router's GK-vs-normal
guess no longer suppresses retrieval; it only changes the wording of the no-sources fallback.

Query typos/ellipsis are handled before search by `query_prep.resolve_turn_context`
(rewrites short/elliptical turns into a standalone intent) and
`query_prep.normalize_query_for_retrieval` (LLM spelling/grammar fix — e.g. "dones" →
"drones"). The CRAG rewrite loop is a further safety net: a weak first retrieval triggers a
`rewrite_query` retry. Neural (`msmarco`) retrieval is also fairly typo-tolerant on its own.

**Constraint verification (`verify_constraints`, on by default).** Topic relevance is not
enough: a query like "Irish dairy water usage" can score "good" on a document about *Italian*
dairy floor-cleaning because the topic overlaps, while the **country constraint is unmet**.
After grading, one LLM call checks each surviving source against the query's specific
constraints (country/region, time, crop, species, named project) and drops the
topical-but-off-constraint ones (e.g. cattle docs for a "sheep" question). It fails open
(keeps all on error/timeout). The synthesis prompt also carries a constraint-fidelity rule:
when the remaining sources still don't match a stated constraint, the answer must say so
explicitly rather than implying they do. Together these turn a confident-but-wrong citation
into an honest "no Ireland-specific source found; here is what the sources actually cover."

**No fabricated sources + honest grounding badge.** Two further safeguards: (a) a
`_NO_FABRICATION_RULE` is appended to *every* system prompt (`_assemble_messages`), so no
mode may invent sources/URLs/report titles/grant numbers — it may cite only the current
turn's `[N]` block, and when asked for sources it wasn't given (e.g. a `history_only`
"can you give me the sources?" follow-up) it points to the per-answer source panel instead
of hallucinating. (b) `grounding` is computed *after* citation extraction and emitted at the
end of the turn: it is `euf_supported` only if the answer actually cited ≥1 source; if EUF
context was retrieved but nothing was cited (the model answered from general knowledge), it
is downgraded to `general_fallback` so the badge never overstates grounding.

**Input-budget trimming (no hard fail on long chats).** The prompt is bounded by
`prompt_cap = min(MAX_INPUT_TOKENS, NUM_CTX − MAX_OUTPUT_TOKENS − 256)`. Retrieved contexts
are small (~2.8k tokens: 5 chunks ≤2000 chars, merged-parent ≤3500); what grows unbounded is
conversation **history**. Rather than erroring when history + contexts exceed the cap, the
controller trims to fit — drop the oldest history turns first, then trailing context chunks
(dropping the matching source so citations stay aligned), emitting a `trim` trace. It only
returns "question too long" if a bare question alone exceeds the cap.

**Personalization (profile + memory), wired to respect ongoing context.** When a user is
authenticated, the controller loads their profile + facts + memory notes from Django
(`UserProfileService`, ported from farm_assistant) **concurrently** with routing/retrieval,
and injects them as **latent background** — `_assemble_messages` frames them as "background
you have learned" and instructs the model to use them only when directly relevant, so the
**live conversation history stays the primary context**. Profile is deliberately *not*
injected on casual/greeting/refusal/clarification turns. After each substantive turn, a
fire-and-forget `process_conversation_turn` extracts and stores new facts, so the assistant
grows more personalized across the conversation and future sessions. The UI's "pause memory"
toggle (`pause_personalization`) skips both the read and the write for that turn.

Note the synergy with input-budget trimming: trimming drops *verbatim* old turns, but the
durable facts extracted from those turns survive in the profile/memory and keep being
injected — so long conversations retain their important context even after old messages
scroll out of the window.

## Django integration (login / logout / persistence)

Ported from `farm_assistant` so this app uses the **same** auth and the **same**
`chat_session` / `chat_message` tables (no Django changes — the `euf` backend stores a
turn regardless of how the answer was produced).

- `app/services/chat_history.py` — `load_chat_state` (GET `/chat/sessions/{id}/`),
  `merge_messages`, and `log_turn_to_backend` (POST `/chat/log-turn/`).
- `app/services/backend_client.py` — proxy helpers (auth headers, upstream relay).
- `app/routers/chat_proxy.py` — the auth + session surface (same paths as farm_assistant):
  `POST /chatbot/api/auth/login` · `POST /chatbot/api/auth/logout` ·
  `GET/POST /chatbot/api/chats` · `GET/PATCH/DELETE /chatbot/api/chats/{id}` ·
  `POST /chatbot/api/chats/{id}/log-turn` · `.../message/{mid}/feedback`.

**In the streaming path**, when `session_id` + auth are present the controller:
1. loads prior history from Django and merges it with any client-supplied history, then
2. after the answer completes, persists the turn via `/chat/log-turn/` (when
   `AUTO_PERSIST_TURNS=true`) with `meta = {model, grounding_mode, latency_ms, sources,
   assistant:{agent_mode, agent_iterations}}` and emits a `session` event.

Auth is read from the `Authorization` header, or an `auth` query param on the GET stream
(browser `EventSource` cannot set headers). Config: `CHAT_BACKEND_URL` (auto-derived from
`FA_ENV`), `AUTH_BACKEND_URL`, `ADMIN_API_TOKEN`, `AUTO_PERSIST_TURNS`.

Typical conversation flow (headless API):
```
POST /chatbot/api/auth/login            -> { token }
POST /chatbot/api/chats                 -> { session_uuid }     (or let log-turn auto-create)
GET  /chatbot/api/chats/{id}/message/stream?q=...&auth=<token>  -> SSE answer, turn persisted
GET  /chatbot/api/chats/{id}            -> session with both messages
```

## Web UI (ported verbatim from farm_assistant)

`templates/` (login.html, ask_stream.html) and `static/` (chat.js, auth.js, login.js,
voice.js, custom.css) are copied unchanged from farm_assistant. `main.py` mounts `/static`
and serves the pages:

- `GET /` → login page (`login.js` POSTs to `/api/login`)
- `GET /chat` and `GET /c/{session_id}` → the chat UI (sidebar, history, streaming,
  cited sources, regenerate, feedback, browser read-aloud, memory modal)

The bundled `chat.js` streams via `POST /chatbot/api/chats[/{id}]/message` and uses the
legacy `/proxy/*` aliases for sessions / logout / **log-turn**, plus
`/chatbot/api/users/me/memory`. All of these are provided by `routers/chat_proxy.py`.
Because `chat.js` logs each turn itself (and reads back the `session_uuid` from log-turn),
**`AUTO_PERSIST_TURNS` is set to `false`** when using the UI to avoid double-writes.

**Not wired (per scope):** PDF upload (`/files/pdf`). The composer's upload button will
error until a files router + PDF service are ported; everything else works.

## Configuration knobs (new `AGENT_*` in `config.py` / `.env`)

| Setting | Default | Effect |
|---|---|---|
| `AGENT_MAX_CORRECTIONS` | `2` | corrective retries after the first search; **`0` ⇒ farm_assistant's single-pass behaviour** |
| `AGENT_GRADE_GOOD` | `0.15` | heuristic overlap ≥ this ⇒ accept (no LLM call) |
| `AGENT_GRADE_BAD` | `0.05` | below this ⇒ clearly weak (skip LLM grader) |
| `AGENT_ENABLE_LLM_GRADER` | `false` | LLM second-opinion for borderline grades |
| `AGENT_ENABLE_CONSTRAINT_FILTER` | `true` | verify each source against the query's constraints (country/time/crop/species/project); drop topical-but-off-constraint hits |
| `AGENT_ENABLE_CONSTRAINT_RECOVERY` | `true` | if constraint-verify empties the sources, retry once with a constraint-targeted query before falling back |
| `AGENT_ENABLE_ANSWER_VERIFICATION` | `true` | Self-RAG: check the grounded answer against its cited sources; append a caveat if it overstates them |
| `AGENT_LLM_GRADE_PASS` | `0.5` | LLM relevance ≥ this ⇒ accept |
| `AGENT_ENABLE_DECOMPOSITION` | `false` | split multi-part questions into sub-queries |
| `AGENT_DROP_WEAK_CONTEXTS` | `true` | drop still-weak contexts after retries (vs. ground in noise) |

## Latency posture (preserved from `farm_assistant`)

- The cheap deterministic guardrails + LLM router run **before** the loop, so off-topic /
  greeting / recap turns never pay for retrieval.
- Grading is **heuristic-first**; the LLM grader is off by default. Most turns add **zero**
  extra LLM calls — a corrective rewrite+research only happens when the first retrieval is
  genuinely weak.
- The loop is hard-bounded by `AGENT_MAX_CORRECTIONS`.

## How to run

```bash
cd agentic_farm_assistant
cp .env.sample .env            # fill OpenSearch + vLLM values
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh                        # -> http://localhost:18002  (docs at /docs)
# stream a turn:
curl -N "http://localhost:18002/ask/stream?q=What%20is%20crop%20rotation%3F"
```

## Verification performed

The loop was exercised offline (network calls stubbed) to confirm wiring:

1. **CRAG recovery** — weak first retrieval (overlap 0.00) → `Refine` → `rewrite_query` →
   second retrieval (overlap 0.70, good) → grounded answer citing `[1]`,
   `grounding=euf_supported`, `iterations=1`.
2. **Direct path** — an off-topic turn routed to `off_topic`, **skipped retrieval**
   entirely (`Routing → Scope → LLM`), `grounding=off_topic`.
3. **Bounded fallback** — persistently weak retrieval looped exactly
   `1 + AGENT_MAX_CORRECTIONS` times, then dropped the weak contexts →
   `grounding=general_fallback`, no citations.

Pure helpers (routing guardrails, citation normalize/strip) were unit-exercised
(e.g. consumer-tech query → `off_topic`; orphan citation `[9]` stripped while `[1]` kept).

## Constraint recovery + answer verification (Self-RAG)

Two final layers on the loop:

- **Constraint recovery** (`AGENT_ENABLE_CONSTRAINT_RECOVERY`): if constraint verification
  drops *all* sources (topic matched but the specific constraint didn't), the controller
  does one constraint-targeted `rewrite_query` → re-retrieve → re-grade → re-verify before
  giving up. Emits a `Recover` status + `recover` trace. Only fires on a total constraint
  miss, so it adds cost only when it can actually help.
- **Answer verification / Self-RAG-lite** (`AGENT_ENABLE_ANSWER_VERIFICATION`): after a
  grounded answer is drafted, `verify_answer_grounding` asks the model whether the answer's
  claims are actually backed by the cited sources (`supported`/`partial`/`unsupported`),
  emitted as a `verification` event. On `partial`/`unsupported` it streams a short honest
  caveat ("parts of this answer draw on general knowledge beyond the cited sources") so the
  answer never overstates its EU-FarmBook backing. One LLM call per grounded turn (fail-open).

## Evaluation harness (`eval/`)

`eval/run_eval.py` runs `eval/questions.jsonl` through the agent **in-process**
(`run_turn`, no server/login) against live OpenSearch + vLLM, and scores each case with
rule-based checks (routing class, grounded/no-sources, every cited source has a provenance
URL, no fabricated links, answered) plus an optional `--judge` LLM score. It prints a
per-case table + summary (pass rate, routing accuracy, grounded rate, fabrication count,
latency), saves `eval/reports/report_<ts>.json` + `latest.json`, and **diffs against the
previous run** to flag regressions / improvements / behaviour changes. The cases encode the
bugs already fixed (off_topic routing of "biosecurity", the Irish-dairy-water constraint
miss, the hallucinated "give me the sources" follow-up) so they can't silently regress.
Run: `python -m eval.run_eval` (see `eval/README.md`). Baseline at creation: 10/11 pass,
routing 0.9, grounded 1.0, 0 fabrications; the one fail is a real router miss — capability
questions ("what can you help with?") sometimes route to general_knowledge/normal.

## Not yet implemented (future phases)

- **Phase 4** self-RAG reflection (groundedness check of the draft vs. citations).
- **Phase 5** multi-source routing (keyword/BM25, attachments, external web fallback).
- Attachments (PDF/image) and Django-backed personalization were intentionally left out of
  this first cut; they slot in as additional tools (`read_attachments`, `recall_profile`)
  without changing the loop.
