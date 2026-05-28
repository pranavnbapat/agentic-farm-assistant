# 04 — Migration Roadmap

A phased, low-risk path from the current `farm_assistant` to the agentic design. Each phase
is independently shippable and reuses existing code. Phases are ordered by
**impact-per-effort** — the biggest quality win (the corrective loop) comes early.

## Guiding rule

> Never break the working system. Build the agent controller **beside** `ask_stream`,
> behind a flag, and reuse the same services. The existing linear path remains the fallback.

## Phase 0 — Scaffolding (no behaviour change)

- Create `agentic_farm_assistant/app/` with the layout in
  [03-target-architecture.md §8](./03-target-architecture.md).
- Add `tools/` adapters that simply call the existing `farm_assistant` services
  (import or copy them). No new logic — just stable tool signatures:
  `search_eufarmbook`, `grade_relevance`, `rewrite_query`, `synthesize_answer`.
- Add an `AgentState` dataclass.

**Exit check:** the tool adapters reproduce today's behaviour when called in sequence
(retrieve → synthesize) with identical output to the current `normal` path.

## Phase 1 — Corrective loop (CRAG) — *the headline feature*

Replace the `estimate_retrieval_quality < 0.15 → give up` branch with a bounded loop:

```text
search → grade → if weak and iterations_left: rewrite_query(reason) → re-search
                 else: proceed with what we have (or general_fallback)
```

- Iteration cap = **2**; thresholds in `policies.py`.
- Grading: heuristic first (`estimate_retrieval_quality`); only call an LLM grader in a
  borderline band (e.g. 0.10–0.25).
- Emit `Refine` status on each retry.

**Why first:** highest quality gain, smallest surface, reuses `rewrite_query` and the
existing grade signal. This alone makes the system meaningfully "agentic."

**Exit check:** queries that previously fell back to a disclaimer now recover via one
rewrite+retry on a measurable fraction of cases; p95 latency stays within budget because
most turns never enter a retry.

## Phase 2 — Entry policy integration + streaming status

- Route through the existing `_route_turn_mode` **before** the loop so non-retrieval modes
  bypass it entirely (preserves latency + the deterministic guardrails).
- Wire the agent steps to the SSE `status` channel per
  [03 §6](./03-target-architecture.md#6--streaming--ux-mapping).
- Put the whole agent path behind a request flag / env toggle so it can be A/B'd against
  the current `ask_stream`.

**Exit check:** off-topic/greeting/recap turns never trigger retrieval; status events
narrate each step; flag flips cleanly between old and new paths.

## Phase 3 — Question decomposition (planner)

- Add `decompose_question` for multi-part queries; run sub-queries (concurrently where safe),
  then merge evidence with the existing parent-dedup logic from `context_service`.
- Keep single-query fast-path for the common case (no decomposition overhead).

**Exit check:** a compound question retrieves distinct evidence per part and cites both.

## Phase 4 — Self-RAG-lite reflection

- After synthesis, run a groundedness check of the draft against its cited sources.
- If a key claim is unsupported and an iteration remains, do one targeted re-retrieve;
  otherwise hedge the unsupported claim.
- Emit `Verify` status.

**Exit check:** reduced rate of confidently-stated unsupported claims (measure on the
existing eval set — see note below).

## Phase 5 — Multi-source routing (optional / future)

- Add source escalation order in `policies.py`: EU-FarmBook neural → keyword/BM25 →
  attachments → (optionally) external `web_search`.
- The grader decides when to escalate, not just rewrite.

**Exit check:** queries with no EU-FarmBook coverage can be answered from an alternate
source with correct provenance labelling.

## Cross-cutting: evaluation & guardrails

- **Reuse existing eval harnesses.** The repo already has `chatbot_model_evaluation/`,
  `blind_chatbot_evaluation/`, and `llm_evaluator/` — point them at the agentic endpoint to
  compare answer quality, grounding rate, and latency against the current system before
  flipping the flag.
- **Keep latency telemetry.** Extend the existing `timing` SSE event with per-iteration and
  per-tool timings so regressions are visible.
- **Preserve safety gates.** The deterministic off-topic/injection guardrails stay as a
  pre-loop filter throughout.

## Suggested sequencing summary

| Phase | Deliverable | Reuses | New code | Risk |
|---|---|---|---|---|
| 0 | Tool adapters + state | all services | tiny | none |
| 1 | **CRAG corrective loop** | grade + rewrite | controller core | low |
| 2 | Entry routing + status streaming | `_route_turn_mode`, SSE | wiring | low |
| 3 | Decomposition | context merge | planner tool | medium |
| 4 | Reflection | citation machinery | grader prompt | medium |
| 5 | Multi-source | — | connectors | medium/high |

Ship Phase 1 to get the bulk of the agentic benefit; Phases 3–5 are progressive enhancements.
