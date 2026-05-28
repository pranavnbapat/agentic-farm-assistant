# Agentic Farm Assistant

An **Agentic RAG** evolution of `farm_assistant`. Instead of a single forward pass, each
chat turn runs a bounded controller loop:

```
route → (plan) → [ search → grade → correct ]* → synthesize
```

The key behavioural change: when retrieval is weak, the system **rewrites the query and
retries** (Corrective RAG) before answering — where `farm_assistant` would immediately fall
back to a disclaimed general-knowledge answer.

It reuses `farm_assistant`'s building blocks — OpenSearch `/llm_retrieve`, vLLM/Qwen3,
the ranking/quality services, and the per-mode prompt builders — wrapped as **tools** behind
a new agent controller. `farm_assistant` itself is **not modified or imported**; the shared
code was copied so this app is standalone.

## Quick start

```bash
cp .env.sample .env          # set OPENSEARCH_API_URL + VLLM_URL + creds
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh                     # http://localhost:18002

# Then open the web UI in a browser:
#   http://localhost:18002/        -> login page (EU-FarmBook account)
#   after login -> /chat            -> full chat UI (ported from farm_assistant)
# API docs are at /docs; raw SSE works headless too:
curl -N "http://localhost:18002/ask/stream?q=What%20is%20crop%20rotation%3F"
```

## How it works

- **Entry routing** keeps off-topic / greeting / recap turns out of the loop (no wasted
  retrieval).
- **CRAG loop** (for grounding-worthy questions): retrieve → grade relevance → on a weak
  grade, rewrite & re-search (bounded by `AGENT_MAX_CORRECTIONS`, default 2).
- **Cheap-first grading**: a token-overlap heuristic decides most turns; an optional LLM
  grader (`AGENT_ENABLE_LLM_GRADER`) handles borderline cases.
- **Streaming**: every agent step emits an SSE `status` and `trace` event; the answer
  streams as `token` events with cited `sources` at the end.

Set `AGENT_MAX_CORRECTIONS=0` to reproduce `farm_assistant`'s original single-pass behaviour.

## Documentation

Full analysis, design rationale, and implementation notes are in **[`docs/`](./docs/)**:

| Doc | Contents |
|---|---|
| [docs/README.md](./docs/README.md) | Executive verdict + index |
| [01-current-architecture.md](./docs/01-current-architecture.md) | How `farm_assistant` works today |
| [02-agentic-assessment.md](./docs/02-agentic-assessment.md) | What's already agentic vs. missing |
| [03-target-architecture.md](./docs/03-target-architecture.md) | The agentic pipeline + tool catalog |
| [04-migration-roadmap.md](./docs/04-migration-roadmap.md) | Phased build plan |
| [05-implementation.md](./docs/05-implementation.md) | What was built, event protocol, how to run |

## Status

Implemented: Phases 0–2 (tool adapters, the bounded CRAG controller, entry routing +
streaming status). Future: self-RAG reflection (Phase 4), multi-source routing (Phase 5),
attachments + personalization tools.
