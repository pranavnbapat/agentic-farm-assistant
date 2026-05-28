# 06 — Architecture (high-level)

A visual map of the Agentic Farm Assistant: components, the agent's decision flow, and the
data that moves through a turn. All diagrams use plain ASCII so they render identically
everywhere. Everything here reflects the actual implementation in `app/`.

---

## 1. System context

What talks to what. The FastAPI app is the brain; OpenSearch, vLLM, and Django are
external services it orchestrates.

```text
┌────────────────────────────────────────────────────────────────────────────┐
│  BROWSER                                                                     │
│   login.html ──login──▶                                                      │
│   ask_stream.html + chat.js  ──SSE (fetch+ReadableStream, JWT header)──▶      │
└───────────────┬──────────────────────────────────────────────────────────────┘
                │  HTTP / SSE
                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  FastAPI app (app/main.py)                                                   │
│                                                                              │
│   routers/ask.py        → drives the AGENT CONTROLLER, relays SSE            │
│   routers/chat_proxy.py → login/logout, chat-session CRUD, log-turn, memory  │
│                                                                              │
│   ┌──────────────────────  agent/controller.py : run_turn  ───────────────┐ │
│   │  the bounded loop (see §2)                                            │ │
│   │  uses tools/  →  routing · query_prep · retrieve · grade · synthesize │ │
│   │  uses services/ → search · context · prompt · chat_history · profile  │ │
│   └───────────────────────────────────────────────────────────────────────┘ │
└───────┬───────────────────────┬───────────────────────────┬──────────────────┘
        │ /llm_retrieve         │ /v1/chat/completions      │ /fastapi/login, /chat/*
        ▼                       ▼                           ▼
┌───────────────┐      ┌────────────────────┐     ┌──────────────────────────────┐
│  OpenSearch   │      │  vLLM (Qwen3-30B)  │     │  Django backend (euf)         │
│  neural RAG   │      │  routing · rewrite │     │  auth (JWT)                   │
│  retrieval    │      │  grade · verify    │     │  chat_session / chat_message  │
│               │      │  synthesis stream  │     │  user profile / facts / memory│
└───────────────┘      └────────────────────┘     └──────────────────────────────┘
```

---

## 2. The agentic turn — decision flow

The heart of the system: a bounded **route → retrieve → grade → correct → verify →
synthesize → verify-answer** loop. Each numbered stage is a decision point.

```text
  user turn
     │
     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 1. RESOLVE     rewrite the turn into a standalone query                    │
│                (skipped when the message is already standalone)            │
└──────────────────────────────────────┬───────────────────────────────────┘
                                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 2. ROUTE       hard guardrail (deterministic)  →  else LLM 7-mode router   │
│                                                                            │
│    decision ─┬─ off_topic · conversation · history · capability · clarify  │
│              │        └────────────────────────────▶ DIRECT ANSWER ──▶ (5) │
│              └─ normal · general_knowledge ─────────▶ RETRIEVAL PATH ──▶ (3)│
└──────────────────────────────────────┬───────────────────────────────────┘
                                        ▼  (retrieval path)
┌──────────────────────────────────────────────────────────────────────────┐
│ 3. RETRIEVE & CORRECT   bounded loop, ≤ AGENT_MAX_CORRECTIONS              │
│                                                                            │
│     normalize query ─▶ OpenSearch /llm_retrieve ─▶ GRADE relevant?         │
│            ▲                                            │                  │
│            └──────────── weak & budget left ◀─ rewrite ─┤                  │
│                                       good / budget out ▼                  │
└──────────────────────────────────────┬───────────────────────────────────┘
                                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 4. VERIFY SOURCES   drop topical-but-off-constraint sources                │
│                     (e.g. an Italian doc for an "Irish" question)          │
│                                                                            │
│     all sources dropped? ─ yes & recovery on ─▶ constraint-targeted retry  │
│                          ─ no ─────────────────▶ continue                  │
└──────────────────────────────────────┬───────────────────────────────────┘
                                        ▼
                        sources survive? ─── no ──▶ GENERAL FALLBACK
                                        │           (honest "no EU-FarmBook source")
                                  yes   │                       │
                                        ▼                       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 5. SYNTHESIZE   trim history to token budget · stream the answer via vLLM  │
│                 (DIRECT ANSWER and GENERAL FALLBACK also enter here)       │
└──────────────────────────────────────┬───────────────────────────────────┘
                                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 6. VERIFY ANSWER (Self-RAG)   are the claims supported by the sources?     │
│                  ─ partial / unsupported ─▶ append an honest caveat        │
└──────────────────────────────────────┬───────────────────────────────────┘
                                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 7. FINALIZE   citation hygiene · honest grounding badge ·                  │
│               persist the turn + learn facts (Django + profile)            │
└──────────────────────────────────────┬───────────────────────────────────┘
                                        ▼
                                 done (SSE complete)
```

---

## 3. Decision points (who decides, on what)

| # | Decision | How | Outcome |
|---|---|---|---|
| 1 | Standalone vs follow-up | structural (length/words) | skip or run query resolution |
| 2 | Hard guardrail | deterministic term/regex match | force `off_topic`/`clarify`/`capability`, or defer |
| 3 | Turn mode (7-way) | **LLM** classifier (cheap, 1.5s budget) | retrieval vs direct-answer path |
| 4 | Retrieval relevant? | heuristic token-overlap **+ LLM** if borderline | accept, or rewrite & retry |
| 5 | Corrections left? | counter (`AGENT_MAX_CORRECTIONS`) | loop or stop |
| 6 | Constraint match | **LLM** per-source check | drop off-constraint sources |
| 7 | Recovery needed? | all sources dropped? | one constraint-targeted retry |
| 8 | Grounded? | any source survived + cited? | `euf_supported` vs fallback |
| 9 | History fits budget? | token estimate vs cap | trim oldest turns / contexts |
| 10 | Answer supported? | **LLM** Self-RAG check | append honest caveat or not |

---

## 4. Data flow through one turn

Numbered exchanges between the controller and the services it orchestrates. Single
arrows `──▶` are request/response; double arrows `══▶` are the SSE stream to the browser.

```text
 ①  Browser     ──(q + JWT, client_history)──▶  Controller
 ②  Controller  ──(load history + profile)────▶  Django      ──(prior msgs, facts, memory)──▶ Controller
 ③  Controller  ──(route + normalize query)───▶  vLLM        ──(turn mode, clean query)──────▶ Controller
 ④  Controller  ──(search_term)───────────────▶  OpenSearch  ──(candidate KOs: ctx+sources)──▶ Controller
 ⑤  Controller  ──(grade / rewrite, looped)───▶  vLLM            [bounded by MAX_CORRECTIONS]
 ⑥  Controller  ──(constraint verify sources)─▶  vLLM
 ⑦  Controller  ──(synthesize, streaming)─────▶  vLLM        ══(answer tokens, SSE)══════════▶ Browser
 ⑧  Controller  ──(Self-RAG answer check)─────▶  vLLM
 ⑨  Controller  ══(verification · sources · grounding · timing · done)═════════════════════▶ Browser
 ⑩  Controller  ──(persist turn + learn facts, fire-and-forget)──▶ Django
```

What each component contributes:

```text
┌──────────────┬────────────────────────────────────────────────────────────┐
│ OpenSearch   │ candidate knowledge objects (title, project, description,    │
│              │ content chunks) — the raw evidence.                          │
├──────────────┼────────────────────────────────────────────────────────────┤
│ vLLM (Qwen3) │ every DECISION (route, grade, constraint/answer verify) and  │
│              │ the ANSWER (streamed). The reasoning engine.                 │
├──────────────┼────────────────────────────────────────────────────────────┤
│ Django       │ durable context IN (prior turns, profile/facts/memory) and   │
│              │ the completed turn + newly-learned facts OUT.                │
├──────────────┼────────────────────────────────────────────────────────────┤
│ Controller   │ orchestration + AgentState: evolving query, evidence,        │
│ (run_turn)   │ grounding label, and a step-by-step trace.                   │
└──────────────┴────────────────────────────────────────────────────────────┘
```

---

## 5. SSE event stream (what the UI receives)

```text
status*  →  trace*  →  token*  →  verification?  →  grounding  →  sources  →  timing  →  done
                                                                                  (or app_error)
```

| event | payload | meaning |
|---|---|---|
| `status` | `{stage, message}` | live step: Routing/Search/Assess/Refine/Verify/Recover/LLM/Verify-answer |
| `trace` | `{step, detail, …}` | agent step record (route/search/grade/rewrite/verify/recover/verify_answer/trim/synthesize) |
| `token` | text | streamed answer chunk (caveat appended here too) |
| `verification` | `{verdict, note}` | Self-RAG result for grounded answers |
| `grounding` | `{mode}` | `euf_supported` / `general_fallback` / `general_knowledge` / direct mode |
| `sources` | `[{n, title, url, …}]` | the cited subset only |
| `timing` | `{total_ms, iterations, mode, grounding}` | telemetry |
| `session` | `{session_id}` | session a turn was persisted to |
| `done` / `app_error` | — | end / error |

---

## 6. The control knobs (`AGENT_*`)

The behaviour above is policy-driven; each decision has a switch (`app/agent/policies.py`,
set via `.env`).

| Knob | Default | Governs |
|---|---|---|
| `AGENT_MAX_CORRECTIONS` | `2` | retrieval retry budget (decision #5); `0` = single-pass |
| `AGENT_GRADE_GOOD` / `_BAD` | `0.15` / `0.05` | relevance accept / borderline band (#4) |
| `AGENT_ENABLE_LLM_GRADER` | `false` | LLM second-opinion on borderline grades (#4) |
| `AGENT_ENABLE_CONSTRAINT_FILTER` | `true` | per-source constraint check (#6) |
| `AGENT_ENABLE_CONSTRAINT_RECOVERY` | `true` | retry on total constraint miss (#7) |
| `AGENT_ENABLE_ANSWER_VERIFICATION` | `true` | Self-RAG answer check (#10) |
| `AGENT_ENABLE_DECOMPOSITION` | `false` | split multi-part questions |
| `AGENT_DROP_WEAK_CONTEXTS` | `true` | don't ground in noise after retries (#8) |
| `AUTO_PERSIST_TURNS` | `false`* | server-side turn persistence (*UI logs turns itself) |

---

## 7. One-line summary

> A domain-scoped **agentic RAG**: an LLM-driven controller routes each turn, retrieves
> from EU-FarmBook, and **verifies relevance, constraints, and its own answer** — correcting
> or honestly hedging at each step — then streams a grounded, cited reply and learns from the
> turn. Bounded by explicit policies so it stays fast and predictable.

See [03-target-architecture.md](./03-target-architecture.md) for the design rationale and
[05-implementation.md](./05-implementation.md) for the code-level detail.
