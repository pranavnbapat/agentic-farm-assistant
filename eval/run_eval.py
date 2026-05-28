#!/usr/bin/env python3
"""
Agentic Farm Assistant — evaluation harness.

Runs a set of EU-FarmBook questions through the agent IN-PROCESS (via
app.agent.controller.run_turn) against the live OpenSearch + vLLM backends — no
server or login needed. Captures routing/grounding/citation/latency signals,
applies rule-based checks (plus an optional LLM-as-judge), writes a JSON report,
and diffs against the previous run to flag regressions.

Usage (from the agentic_farm_assistant/ dir, with deps available):
    python -m eval.run_eval                     # rule-based checks
    python -m eval.run_eval --judge             # + LLM-as-judge answer scoring
    python -m eval.run_eval --questions eval/questions.jsonl --limit 5

The cases in questions.jsonl deliberately encode bugs we already fixed
(off_topic routing of "biosecurity", hallucinated source lists, etc.) so this
harness guards against regressions.
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make `app` importable when run as `python eval/run_eval.py` too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.controller import run_turn  # noqa: E402
from app.config import get_settings  # noqa: E402

S = get_settings()
EVAL_DIR = Path(__file__).resolve().parent
REPORT_DIR = EVAL_DIR / "reports"
RETRIEVAL_MODES = {"normal", "general_knowledge"}
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def route_class(mode: str | None) -> str:
    if mode in RETRIEVAL_MODES:
        return "retrieval"
    return {
        "off_topic": "off_topic",
        "conversation_only": "conversational",
        "history_only": "recap",
        "assistant_capabilities": "capability",
        "clarification_only": "clarify",
    }.get(mode or "", mode or "unknown")


async def run_case(case: dict) -> dict:
    """Run one question through the agent and collect raw signals."""
    events: list[tuple[str, object]] = []
    t0 = time.perf_counter()
    err = None
    async for ev in run_turn(
        user_q=case["question"],
        history_messages=case.get("history") or [],
    ):
        events.append((ev["event"], ev["data"]))
    wall_ms = int((time.perf_counter() - t0) * 1000)

    mode = grounding = None
    cited: list[dict] = []
    answer_parts: list[str] = []
    trace_steps: list[str] = []
    iterations = 0
    for e, d in events:
        if e == "token":
            answer_parts.append(d)
        elif e == "grounding":
            grounding = d.get("mode")
        elif e == "sources":
            cited = d or []
        elif e == "app_error":
            err = d
        elif e == "timing":
            mode = d.get("mode")
            iterations = d.get("iterations", 0)
        elif e == "trace":
            trace_steps.append(d.get("step"))

    answer = "".join(answer_parts)
    cited_urls = {s.get("url") for s in cited if s.get("url")}
    answer_urls = set(_URL_RE.findall(answer))
    fabricated = sorted(u for u in answer_urls if u not in cited_urls)

    return {
        "id": case["id"],
        "question": case["question"],
        "category": case.get("category"),
        "mode": mode,
        "route_class": route_class(mode),
        "grounding": grounding,
        "n_cited": len(cited),
        "source_urls": sorted(u for u in cited_urls if u),
        "fabricated_urls": fabricated,
        "latency_ms": wall_ms,
        "iterations": iterations,
        "verified": "verify" in trace_steps,
        "trimmed": "trim" in trace_steps,
        "error": err,
        "answer_excerpt": answer[:300],
        "answer_len": len(answer),
    }


def evaluate(case: dict, r: dict) -> dict:
    """Rule-based pass/fail checks. None = not applicable to this case."""
    checks: dict[str, bool | None] = {}

    exp_class = case.get("expect_route_class")
    checks["routing"] = (r["route_class"] == exp_class) if exp_class else None

    exp_grounded = case.get("expect_grounded")
    if exp_grounded is True:
        checks["grounded"] = (r["grounding"] == "euf_supported" and r["n_cited"] > 0)
    elif exp_grounded is False:
        checks["no_sources"] = (r["n_cited"] == 0)
    # exp_grounded None -> no grounding assertion

    # Cited sources come from retrieval by construction; the meaningful bar is that each
    # cited source carries a clickable provenance URL (so the user can verify it).
    if r["n_cited"] > 0:
        checks["sources_have_url"] = (len(r["source_urls"]) == r["n_cited"])

    # No fabricated external links (anti-fabrication guard).
    checks["no_fabrication"] = (len(r["fabricated_urls"]) == 0)

    # Should always produce some answer and no hard error.
    checks["answered"] = (r["error"] is None and r["answer_len"] > 0)

    applicable = {k: v for k, v in checks.items() if v is not None}
    passed = all(applicable.values())
    return {"checks": checks, "passed": passed}


async def llm_judge(case: dict, r: dict) -> int | None:
    """Optional LLM-as-judge: 1-5 for helpfulness + groundedness. Best-effort."""
    from app.clients.vllm_client import generate_once

    titles = "; ".join(t for t in [] if t)  # placeholder; titles not retained per-case
    prompt = (
        "Score the assistant answer to the user question from 1 (poor) to 5 (excellent), "
        "judging helpfulness and whether it avoids unsupported claims. "
        'Return JSON only: {"score": <1-5>}.\n\n'
        f"QUESTION: {case['question']}\n\n"
        f"ANSWER: {r['answer_excerpt']}\n\nJSON:"
    )
    try:
        raw = await asyncio.wait_for(generate_once(prompt, 0.0, 12), timeout=8.0)
        m = re.search(r'"score"\s*:\s*([1-5])', raw or "")
        return int(m.group(1)) if m else None
    except Exception:
        return None


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    routing = [r for r in rows if r["eval"]["checks"].get("routing") is not None]
    grounded = [r for r in rows if r["eval"]["checks"].get("grounded") is not None]
    lat = [r["latency_ms"] for r in rows]
    judges = [r["judge"] for r in rows if r.get("judge") is not None]

    return {
        "n": n,
        "pass_rate": round(sum(1 for r in rows if r["eval"]["passed"]) / n, 3) if n else 0,
        "routing_accuracy": round(sum(1 for r in routing if r["eval"]["checks"]["routing"]) / len(routing), 3) if routing else None,
        "grounded_rate": round(sum(1 for r in grounded if r["eval"]["checks"]["grounded"]) / len(grounded), 3) if grounded else None,
        "fabrication_cases": sum(1 for r in rows if r["fabricated_urls"]),
        "mean_latency_ms": int(statistics.mean(lat)) if lat else 0,
        "median_latency_ms": int(statistics.median(lat)) if lat else 0,
        "max_latency_ms": max(lat) if lat else 0,
        "judge_mean": round(statistics.mean(judges), 2) if judges else None,
    }


def load_previous() -> dict | None:
    latest = REPORT_DIR / "latest.json"
    if latest.exists():
        try:
            return json.loads(latest.read_text())
        except Exception:
            return None
    return None


def print_diff(prev: dict | None, rows: list[dict]):
    if not prev:
        print("\n(no previous report to diff against)")
        return
    prev_by_id = {c["id"]: c for c in prev.get("cases", [])}
    regressions, improvements, changes = [], [], []
    for r in rows:
        p = prev_by_id.get(r["id"])
        if not p:
            continue
        was, now = p["eval"]["passed"], r["eval"]["passed"]
        if was and not now:
            regressions.append(r["id"])
        elif now and not was:
            improvements.append(r["id"])
        if p.get("mode") != r["mode"] or p.get("grounding") != r["grounding"] or p.get("n_cited") != r["n_cited"]:
            changes.append(f"{r['id']}: mode {p.get('mode')}->{r['mode']}, ground {p.get('grounding')}->{r['grounding']}, cited {p.get('n_cited')}->{r['n_cited']}")
    print("\n=== DIFF vs previous run ===")
    print("  REGRESSIONS (pass->fail):", regressions or "none")
    print("  improvements (fail->pass):", improvements or "none")
    if changes:
        print("  behaviour changes:")
        for c in changes:
            print("   -", c)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(EVAL_DIR / "questions.jsonl"))
    ap.add_argument("--judge", action="store_true", help="also run LLM-as-judge scoring")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    args = ap.parse_args()

    cases = [json.loads(line) for line in Path(args.questions).read_text().splitlines() if line.strip()]
    if args.limit:
        cases = cases[: args.limit]

    prev = load_previous()  # read BEFORE we overwrite latest.json
    rows = []
    print(f"Running {len(cases)} cases against model={S.VLLM_MODEL} ...\n")
    print(f"{'id':<22} {'route':<14} {'ground':<16} {'cit':>3} {'ms':>6}  result")
    print("-" * 78)
    for case in cases:
        r = await run_case(case)
        r["eval"] = evaluate(case, r)
        if args.judge:
            r["judge"] = await llm_judge(case, r)
        rows.append(r)
        flag = "PASS" if r["eval"]["passed"] else "FAIL"
        fab = "  ⚠FABRICATED" if r["fabricated_urls"] else ""
        print(f"{r['id']:<22} {r['route_class']:<14} {str(r['grounding']):<16} {r['n_cited']:>3} {r['latency_ms']:>6}  {flag}{fab}")
        if not r["eval"]["passed"]:
            failed = [k for k, v in r["eval"]["checks"].items() if v is False]
            print(f"    failed checks: {failed}")

    summary = summarize(rows)
    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k:<20}: {v}")

    print_diff(prev, rows)

    REPORT_DIR.mkdir(exist_ok=True)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": S.VLLM_MODEL,
        "summary": summary,
        "cases": rows,
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (REPORT_DIR / f"report_{ts}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (REPORT_DIR / "latest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nSaved report_{ts}.json (and latest.json)")


if __name__ == "__main__":
    asyncio.run(main())
