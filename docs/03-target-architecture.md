# 03 — Target Agentic RAG Architecture

The proposed design turns the current linear pipeline into a **bounded controller loop**
that reuses `farm_assistant`'s services as tools. It keeps the existing strengths
(routing guardrails, SSE streaming, citation hygiene, personalization) and adds the
missing one: the ability to *act* on what it observes.

## 1. Design principles

1. **Wrap, don't rewrite.** Existing services become tools; the agent loop is the only new
   core component.
2. **Bounded autonomy.** A hard iteration cap (default **2 corrective loops**) protects the
   latency budget. The agent is a *constrained* decision-maker, not free-roaming.
3. **Cheap-first grading.** Use the existing heuristic gate first; spend an LLM grader only
   on borderline cases.
4. **Keep the deterministic gate.** Off-topic / trivial / injection turns are rejected
   *before* the loop, exactly as today.
5. **Stream the reasoning.** Every agent step emits an SSE `status` event so the loop is
   transparent and perceived latency stays low.

## 2. High-level architecture

```text
                          ┌──────────────────────────────────────────────┐
  user turn  ─────────────►            ENTRY POLICY (pre-loop)            │
                          │   reuse _hard_route_turn_mode + _route_turn_mode│
                          └───────┬───────────────────────────────┬──────┘
                                  │ non-retrieval modes            │ "needs grounding"
                                  │ (off_topic, conversation_only, │
                                  ▼  history_only, capabilities…)  ▼
                          ┌───────────────┐          ┌──────────────────────────────────┐
                          │ direct answer │          │           AGENT CONTROLLER         │
                          │ (prompt_svc)  │          │            (bounded loop)          │
                          └───────┬───────┘          │                                    │
                                  │                  │  plan → act(tool) → observe → judge │
                                  │                  │        ▲                    │       │
                                  │                  │        └──── correct ◄──────┘       │
                                  │                  └───────────────┬────────────────────┘
                                  │                                  │ synthesize
                                  ▼                                  ▼
                          ┌──────────────────────────────────────────────────────────────┐
                          │   SYNTHESIS + CITATION HYGIENE  (build_messages + strip)      │
                          │            stream tokens / sources / done over SSE            │
                          └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼  (fire-and-forget, unchanged)
                          profile/fact update + title generation
```

The **entry policy is the existing router** — it cheaply diverts the ~majority of turns
(greetings, off-topic, recaps) away from the loop. Only turns that need grounding
(`normal` / `general_knowledge`-borderline) enter the agent controller.

## 3. The agent controller loop

```text
INPUT: resolved_query, history, profile, attachments, iteration_cap = 2

state = { query, sub_queries: [], evidence: [], notes: [] }

1. PLAN
   - If the query is multi-part → decompose_question() into sub_queries.
   - Else sub_queries = [query].
   emit status: "Planning…"

2. For each sub_query (and on each retry):
   a. ACT:   search_eufarmbook(sub_query, k)        emit status: "Searching…"
   b. OBSERVE: build contexts + sources             (context_service)
   c. JUDGE:  grade = grade_relevance(sub_query, contexts)
              - cheap path: estimate_retrieval_quality (existing)
              - if borderline: llm_grade_relevance (new, sparing)
      emit status: "Checking relevance…"
   d. DECIDE:
        if grade GOOD            → keep evidence, continue
        elif iterations_left > 0 → CORRECT:
             • rewrite_query(sub_query, reason)  → retry (a)   emit "Refining query…"
             • or switch_source()  (attachments / keyword / future web)
        else                     → mark sub_query as "unsupported", continue

3. SYNTHESIZE
   - Merge evidence across sub_queries (dedupe by parent_id — reuse context_service logic).
   - grounding_state = euf_supported | attachment_supported | general_fallback
   - build_messages(...) with has_relevant_sources = bool(evidence)
   - stream answer; run citation hygiene on the final text.

4. (optional) REFLECT  [Self-RAG-lite, can be phase 2]
   - groundedness_check(answer, cited_sources): is each claim supported?
   - if a key claim is unsupported AND iterations_left > 0 → one more targeted retrieve.
```

### Iteration cap math (latency)
Worst case with cap = 2: `route (1 LLM) + [search + grade]×3 + synthesis stream`.
Grading uses the **heuristic first**, so most turns add **0 extra LLM calls**; only
genuinely weak retrievals pay for a rewrite + re-search. This keeps the common path close
to today's latency while fixing the bad-retrieval tail.

## 4. Tool catalog (wrapping existing code)

Each tool is a thin adapter over code that already exists. This is what makes the migration
cheap.

| Tool | Wraps (current code) | New work |
|---|---|---|
| `route_turn(query, history)` | `_route_turn_mode` / `_decide_turn_strategy` | none (reuse) |
| `decompose_question(query)` | — | small LLM prompt → list of sub-queries |
| `search_eufarmbook(query, k)` | `build_search_payload` + `collect_os_items` + `build_context_and_sources` | none (compose) |
| `grade_relevance(query, contexts)` | `estimate_retrieval_quality` + `filter_items_by_min_score` | add LLM grader for borderline band |
| `rewrite_query(query, reason)` | `_resolve_turn_context` + `_normalize_query_for_retrieval` | accept a `reason` (e.g. "too few hits", "off-target") |
| `read_attachments(doc_ids, query)` | `build_pdf_contexts` / `build_image_contexts` | none (reuse) |
| `recall_history(query)` | `chat_history.format_history` | none |
| `recall_profile(user)` | `UserProfileService.build_profile_context` | none |
| `synthesize_answer(query, evidence)` | `build_messages` + `stream_generate` + citation strip | none |
| `web_search(query)` *(future)* | — | new external connector (CRAG fallback source) |

> **Two ways to drive the loop** — pick based on how much control vs. flexibility you want:
>
> - **(A) Orchestrated state machine (recommended first).** A hand-rolled controller (or
>   LangGraph) calls the tools in code; the LLM is consulted only at decision points
>   (route, grade, rewrite, reflect). **Deterministic, easy to bound, streams cleanly,
>   matches the codebase's existing hand-rolled style and latency discipline.**
> - **(B) Native tool-calling agent.** Pass `tools=[...]` to vLLM/Qwen3 and let the model
>   emit tool calls in a ReAct loop. More flexible for open-ended queries, but less
>   predictable latency and harder to bound — better as a **phase-2 option** behind (A).

## 5. Which agentic patterns to adopt

The design is a composition of three well-known patterns, all of which the codebase is
already primed for:

1. **Routing** *(already built)* — keep `_route_turn_mode` as the entry policy.
2. **Corrective RAG (CRAG)** *(the core addition)* — retrieve → grade → on weak grade:
   rewrite & retry, or switch source, with a fallback. This directly upgrades the existing
   `estimate_retrieval_quality < 0.15 → give up` branch into a corrective loop.
3. **Self-RAG-lite reflection** *(phase 2)* — after drafting, verify the answer is grounded
   in its citations; one targeted re-retrieve if a key claim is unsupported. Reuses the
   existing citation machinery.

Question decomposition (planner) is layered on top for multi-part queries.

## 6. Streaming / UX mapping

The agent's transparency rides entirely on the **existing SSE `status` event** — no new
transport needed. Suggested mapping:

| Agent step | SSE `status.stage` | message |
|---|---|---|
| route | `Routing` | "Understanding the question…" |
| decompose | `Planning` | "Breaking the question into parts…" |
| search | `Search` | "Searching EU-FarmBook sources…" |
| grade | `Assess` | "Checking how relevant the results are…" |
| rewrite + retry | `Refine` | "Refining the search and trying again…" |
| switch source | `Source` | "Looking in uploaded files / other sources…" |
| synthesize | `LLM` | "Composing the answer…" |
| reflect | `Verify` | "Double-checking the answer against sources…" |

`grounding`, `token`, `sources`, `timing`, `done` events stay exactly as they are today.

## 7. What stays identical

- The SSE contract and the browser UI.
- `prompt_service` directive blocks and per-mode prompts (the `normal` builder becomes the
  synthesizer; the non-retrieval builders stay the direct-answer paths).
- Citation normalization / orphan stripping on the final text.
- The asynchronous profile/fact/title post-turn loop.
- The deterministic off-topic / injection guardrails.
- vLLM client and OpenSearch client (the controller calls them through the tool adapters).

## 8. Component inventory for the new module

A suggested layout for `agentic_farm_assistant/` (code to be added later — this folder
currently holds only `docs/`):

```text
agentic_farm_assistant/
├── docs/                      # ← these findings
└── app/                       # (proposed, not yet created)
    ├── agent/
    │   ├── controller.py      # the bounded loop (the only genuinely new logic)
    │   ├── state.py           # AgentState dataclass (query, sub_queries, evidence, notes)
    │   └── policies.py        # iteration cap, grade thresholds, source-escalation order
    ├── tools/
    │   ├── retrieve.py        # search_eufarmbook  (wraps search_service+context_service)
    │   ├── grade.py           # grade_relevance    (heuristic + optional LLM grader)
    │   ├── rewrite.py         # rewrite_query / decompose_question
    │   ├── attachments.py     # read_attachments   (wraps pdf/image services)
    │   └── synthesize.py      # synthesize_answer  (wraps prompt_service + vllm stream)
    └── routers/
        └── ask.py             # thin SSE endpoint that drives the controller
```

Most files under `tools/` are <50 lines of adapter code over the existing `farm_assistant`
services.

See [04-migration-roadmap.md](./04-migration-roadmap.md) for the phased build order.
