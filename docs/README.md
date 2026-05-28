# Agentic Farm Assistant — Findings & Architecture

This folder documents an analysis of the existing `farm_assistant` RAG application and
a proposed evolution of it into an **Agentic RAG** system.

> **Source analysed:** `../farm_assistant` (FastAPI + OpenSearch + vLLM/Qwen3 + Django backend)
> **Date:** 2026-05-25

## TL;DR — Can `farm_assistant` become Agentic RAG?

**Yes — and it is an unusually good starting point.** It is *not* a naive single-shot RAG.
It already does query rewriting, multi-mode routing, a retrieval-quality gate, and a
fallback path. What it lacks is the defining feature of agentic RAG: a **decision loop**.
Today the pipeline is a single forward pass — if retrieval is weak, it gives up and falls
back to general knowledge instead of *acting* (re-querying, decomposing, escalating).

The transformation is therefore **evolutionary, not a rewrite**: wrap the existing
services as tools and put a bounded controller loop around them.

| | Naive RAG | **`farm_assistant` today** | Agentic RAG (target) |
|---|---|---|---|
| Retrieve whether needed | always | **routed (7 modes)** ✓ | routed ✓ |
| Query rewriting | no | **yes** ✓ | yes ✓ |
| Retrieval quality check | no | **heuristic gate** ✓ | LLM grader ✓ |
| Re-query on bad results | no | **no** ✗ | yes (loop) ✓ |
| Question decomposition | no | no ✗ | yes ✓ |
| Multi-source routing | no | no ✗ | yes ✓ |
| Self-correction / reflection | no | no ✗ | yes ✓ |

## Documents

1. **[01-current-architecture.md](./01-current-architecture.md)** — How `farm_assistant`
   works today, traced through the actual code (request flow, routing, retrieval, grounding,
   prompt assembly, streaming).
2. **[02-agentic-assessment.md](./02-agentic-assessment.md)** — The verdict in detail:
   which agentic building blocks already exist, which are missing, and why this codebase is
   a strong base.
3. **[03-target-architecture.md](./03-target-architecture.md)** — The proposed Agentic RAG
   pipeline: the controller loop, the tool catalog (wrapping existing services), the
   CRAG/Self-RAG patterns, and how it preserves streaming + latency budgets.
4. **[04-migration-roadmap.md](./04-migration-roadmap.md)** — A phased, low-risk path from
   the current system to the agentic one, reusing existing code at every step.
5. **[05-implementation.md](./05-implementation.md)** — **What was actually built** in
   `app/`: the file tree, the controller loop, the SSE event protocol, config knobs, how to
   run it, and the verification performed. Phases 0–2 of the roadmap are implemented.
6. **[06-architecture.md](./06-architecture.md)** — **High-level architecture diagrams**:
   system context, the agent decision flow, per-turn data flow (sequence), decision-point
   table, SSE event stream, and the control knobs. Start here for the visual overview.

## Key insight

`farm_assistant` already separates concerns cleanly:

- `search_service.py` — OpenSearch orchestration
- `context_service.py` — chunk ranking, source tracking, **quality estimation**
- `prompt_service.py` — per-mode prompt construction
- `ask.py` — the orchestration (`_route_turn_mode`, `_resolve_turn_context`, `_normalize_query_for_retrieval`)

Each of these maps almost 1:1 onto a **tool** in an agentic design. The agent loop is the
only genuinely new component.
