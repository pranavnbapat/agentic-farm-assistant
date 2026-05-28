# Evaluation harness

Runs a small EU-FarmBook test set through the agent **in-process** (via
`app.agent.controller.run_turn`) against the live OpenSearch + vLLM backends — no
server or login required. It scores routing, grounding, citation validity, and
latency with rule-based checks (plus an optional LLM-as-judge), writes a JSON
report, and diffs against the previous run to flag regressions.

## Run

```bash
cd agentic_farm_assistant
source .venv/bin/activate          # needs the app deps + a working .env (OpenSearch + vLLM)
python -m eval.run_eval            # rule-based checks
python -m eval.run_eval --judge    # + LLM-as-judge answer scoring (extra LLM calls)
python -m eval.run_eval --limit 5  # first 5 cases only
```

Each run prints a per-case table + summary and a diff vs the previous run, and
saves `eval/reports/report_<ts>.json` and `eval/reports/latest.json`.

## Test set (`questions.jsonl`)

One JSON object per line:

| field | meaning |
|---|---|
| `id` | stable case id (used for the regression diff) |
| `question` | the user turn |
| `history` | optional prior `[{role, content}]` turns (for follow-up cases) |
| `expect_route_class` | `retrieval` \| `off_topic` \| `conversational` \| `recap` \| `capability` \| `clarify` |
| `expect_grounded` | `true` (must cite EUF) \| `false` (must not cite) \| omit/`null` (no assertion) |

The cases intentionally cover the bugs already fixed — off_topic routing of
"biosecurity", the Irish-dairy-water constraint miss, and the hallucinated
"give me the sources" follow-up — so this harness guards against backsliding.

## Checks (rule-based)

- **routing** — `route_class(mode)` matches `expect_route_class`.
- **grounded** / **no_sources** — citation behaviour matches `expect_grounded`.
- **sources_are_euf** — every cited source URL is a real EU-FarmBook knowledge object.
- **no_fabrication** — the answer contains no external URLs that aren't in the cited sources.
- **answered** — produced an answer with no hard error.

A case passes only if all *applicable* checks pass. The summary reports pass rate,
routing accuracy, grounded rate, fabrication-case count, latency (mean/median/max),
and (with `--judge`) mean judge score.

## Notes

- In-process mode exercises the full agent pipeline without auth, so personalization
  and turn-persistence are no-ops (good for clean, side-effect-free eval).
- To grow the set, just append lines to `questions.jsonl`. Keep ids stable so the
  regression diff stays meaningful.
