# 02 — Agentic Readiness Assessment

**Question:** Can `farm_assistant` be used/turned into an Agentic RAG?
**Verdict:** **Yes.** It already implements ~40% of an agentic design and its clean service
boundaries make the rest an incremental wrap rather than a rewrite.

## 1. What "agentic" requires vs. what exists

Agentic RAG = an LLM-driven controller that **decides and loops**: it chooses whether/what
to retrieve, judges the results, and can take corrective action before answering. Scoring
`farm_assistant` against those pillars:

| Agentic capability | Status | Where it lives today |
|---|---|---|
| **Decide whether to retrieve** | ✅ Present | `_route_turn_mode` (7-mode router) |
| **Query rewriting / contextualization** | ✅ Present | `_resolve_turn_context`, `_normalize_query_for_retrieval` |
| **Retrieve from a knowledge source** | ✅ Present | `search_service` → OpenSearch `/llm_retrieve` |
| **Judge retrieval quality** | 🟡 Partial (heuristic) | `estimate_retrieval_quality`, `filter_items_by_min_score` |
| **Act on bad retrieval (re-query / decompose / escalate)** | ❌ Missing | — (falls back to disclaimer) |
| **Multi-step / iterative retrieval** | ❌ Missing | — (single forward pass) |
| **Question decomposition** | ❌ Missing | — |
| **Route across multiple sources** | ❌ Missing | single endpoint only |
| **Reflection / self-grading of the draft answer** | ❌ Missing | — |
| **Tool abstraction (LLM-selectable actions)** | ❌ Missing | control flow is hardcoded |
| **Bounded autonomy / iteration cap** | ➖ N/A yet | (would be needed) |

## 2. The assets — why this is a strong base

These existing pieces map almost directly onto agentic components:

1. **A router already exists.** `_route_turn_mode` is the "should I retrieve, and how should
   I handle this turn" decision. In an agent design it becomes the **entry policy** that
   keeps off-topic/trivial turns *out of* the expensive loop — a real performance win, since
   you don't want the agent burning iterations on "tell me a joke."

2. **Query transformation already exists.** `_resolve_turn_context` (follow-up resolution)
   and `_normalize_query_for_retrieval` (spelling/grammar) are exactly the `rewrite_query`
   tool an agent would call — they just need to be callable *mid-loop* with a reason.

3. **A grading signal already exists.** `estimate_retrieval_quality` returns a 0–1 score and
   the code already *branches* on it (`< 0.15` → drop). Today that branch only goes one way
   (give up). An agent reuses the same signal as the **loop condition**.

4. **A grounding-state concept already exists.** `euf_supported / attachment_supported /
   general_fallback` is precisely the state an agent uses to decide "good enough vs. act
   again."

5. **The LLM backend supports tool calling.** vLLM + Qwen3 expose OpenAI-style function
   calling. The `vllm_client` is already OpenAI-compatible, so adding a `tools=[...]` path is
   small.

6. **Clean separation of concerns.** search / context / prompt / profile are independent
   services with narrow signatures — trivial to wrap as tools without untangling them.

7. **A streaming `status` channel already exists.** The SSE `status` event already narrates
   "Searching… / Preparing context…". Agent steps ("Refining query…", "Re-searching…",
   "Checking groundedness…") slot straight into it, so the loop stays transparent to the user.

## 3. The gaps — what genuinely must be built

The single defining gap is **the loop**. Everything else follows from it:

- **No controller / agent loop.** `ask_stream` is linear. There is no construct that says
  "given the observation, decide the next action and repeat (up to N times)."
- **No corrective action on weak retrieval.** The most impactful missing behaviour:
  poor `estimate_retrieval_quality` should trigger *rewrite-and-retry* or *decompose* or
  *escalate to another source*, not an immediate fallback.
- **Heuristic grading, not semantic.** Token overlap (`estimate_retrieval_quality`) is fast
  but shallow; it misses paraphrase/synonyms (relevant for an agriculture domain with many
  near-synonyms and multilingual queries). An LLM grader (used sparingly) is more reliable.
- **No decomposition.** Multi-part questions ("compare CAP cover-crop rules *and* summarise
  NETPOULSAFE biosecurity findings") get one query and one retrieval.
- **Single retriever.** Only `/llm_retrieve`. No notion of choosing between indices, a
  keyword vs. neural strategy, attachments-only, or an external fallback.
- **No answer-level reflection.** Nothing checks whether the drafted answer is actually
  grounded in the cited sources before returning.

## 4. Risks / constraints to respect

Any agentic version must stay inside the constraints the current system was tuned for:

- **Latency budget.** The current design is visibly latency-obsessed (1.2–2.0 s timeouts on
  pre-flight hops). Each agent iteration adds at least one LLM round-trip. → The loop must be
  **bounded** (suggest max 2 corrective iterations) and stream `status` so perceived latency
  stays low.
- **Cost.** More LLM calls per turn (grading, rewriting, reflection). → Reserve the
  expensive LLM grader for the cases the cheap heuristic flags as borderline.
- **Determinism / safety.** The deterministic off-topic guardrails are a feature, not debt.
  → Keep them as a pre-loop gate; do not hand scope-control to the model alone.
- **Citation integrity.** The orphan-citation stripping must run on the final synthesized
  answer regardless of how many loops produced it.

## 5. Conclusion

`farm_assistant` is **not** a naive RAG that needs replacing — it is a well-factored
router-RAG that is one component (a bounded controller loop) short of being agentic. The
recommended approach is to **wrap, not rewrite**: expose existing services as tools, add a
controller that loops over *retrieve → grade → correct → synthesize*, and keep every
existing strength (routing guardrails, streaming status, citation hygiene, personalization).

See [03-target-architecture.md](./03-target-architecture.md) for the concrete design.
