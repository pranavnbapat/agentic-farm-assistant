# app/tools/grade.py
#
# GRADE tool — the CRAG decision signal.
# Cheap-first: it uses context_service.estimate_retrieval_quality (token overlap)
# and only escalates to an LLM grader for borderline cases when enabled. This is the
# upgrade over farm_assistant, where a low quality score simply dropped the contexts
# instead of being used as a loop condition to retry.

import asyncio
import json
import logging
import re as _re
from dataclasses import dataclass
from typing import Optional

from app.clients.vllm_client import generate_once
from app.services.context_service import estimate_retrieval_quality

logger = logging.getLogger("agentic-fa.grade")


@dataclass
class GradeResult:
    score: float            # 0..1
    verdict: str            # "good" | "weak" | "empty"
    method: str             # "heuristic" | "llm" | "none"
    detail: str


async def _llm_grade(query: str, contexts: list[str]) -> Optional[float]:
    joined = "\n\n".join(c[:600] for c in contexts[:3])
    prompt = (
        "Rate, from 0 to 1, how well the SOURCES below let you answer the QUESTION. "
        "1 = fully answerable from the sources, 0 = sources are irrelevant. "
        'Return JSON only: {"score": <number 0..1>}.\n\n'
        f"QUESTION: {query}\n\n"
        f"SOURCES:\n{joined}\n\n"
        "JSON:"
    )
    try:
        raw = await asyncio.wait_for(
            generate_once(prompt, temperature=0.0, max_tokens=12),
            timeout=1.8,
        )
        match = _re.search(r"\{.*?\}", raw or "", flags=_re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            score = float(data.get("score"))
            return max(0.0, min(1.0, score))
    except Exception:
        return None
    return None


async def verify_constraints(query: str, sources: list) -> Optional[list[int]]:
    """
    Constraint-aware verification (CRAG/Self-RAG style). Given the query and the
    retrieved sources, return the 0-based indices of sources that actually satisfy the
    SPECIFIC constraints in the question (country/region, time period, crop, species,
    named project) — not just the topic. A source on the right topic but the wrong
    constraint (e.g. an Italian practice for an "Irish" question) is dropped.

    Returns:
      - list[int]  : indices to keep (may be empty if nothing satisfies the constraints)
      - None       : check could not run -> caller should fail-open (keep all sources)
    """
    if not sources:
        return None

    briefs = []
    for i, s in enumerate(sources):
        title = (getattr(s, "title", None) or "").strip()
        proj = (getattr(s, "project", None) or "").strip()
        desc = (getattr(s, "description", None) or "").strip()
        briefs.append(f"[{i}] {title} — {proj} — {desc[:160]}")
    listing = "\n".join(briefs)

    prompt = (
        "A user asked an agricultural question. Below are candidate source documents "
        "(numbered).\n"
        "Return the numbers of the documents that actually satisfy the SPECIFIC "
        "CONSTRAINTS in the question — such as country/region, time period, crop, "
        "animal/species, or a named project.\n"
        "A document on the right TOPIC but the WRONG constraint does NOT satisfy it "
        '(e.g. an Italian practice does not satisfy a question about "Ireland").\n'
        "If the question has no specific constraint, keep every on-topic document.\n\n"
        f"QUESTION: {query}\n\n"
        f"DOCUMENTS:\n{listing}\n\n"
        'Return JSON only: {"keep": [<document numbers that satisfy the constraints>]}'
    )
    try:
        raw = await asyncio.wait_for(
            generate_once(prompt, temperature=0.0, max_tokens=60),
            timeout=3.0,
        )
        match = _re.search(r"\{.*\}", raw or "", flags=_re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        keep_raw = data.get("keep")
        if not isinstance(keep_raw, list):
            return None
        n = len(sources)
        keep = sorted({int(x) for x in keep_raw if isinstance(x, (int, float)) and 0 <= int(x) < n})
        return keep
    except Exception:
        return None


async def verify_answer_grounding(question: str, answer: str, sources: list) -> tuple[str, str]:
    """
    Self-RAG-lite: after a grounded answer is drafted, judge whether its factual claims are
    actually supported by the cited sources.

    Returns (verdict, note):
      - "supported"   : the answer's claims are backed by the sources
      - "partial"     : some claims are supported, others go beyond the sources
      - "unsupported" : key claims are not in the sources
      - "skip"        : check could not run (fail-open; caller should not act on it)
    """
    if not sources or not (answer or "").strip():
        return "skip", ""

    briefs = []
    for i, s in enumerate(sources, 1):
        title = (getattr(s, "title", None) or "").strip()
        proj = (getattr(s, "project", None) or "").strip()
        desc = (getattr(s, "description", None) or "").strip()
        briefs.append(f"[{i}] {title} — {proj} — {desc[:160]}")
    listing = "\n".join(briefs)

    prompt = (
        "Judge how well the ASSISTANT ANSWER's factual claims are supported by the SOURCES "
        "it was given.\n"
        '- "supported": the substantive claims are backed by the sources.\n'
        '- "partial": some claims are supported but others go beyond the sources '
        "(general knowledge presented alongside the cited material).\n"
        '- "unsupported": key claims are not found in the sources.\n\n'
        f"QUESTION: {question}\n\n"
        f"ASSISTANT ANSWER:\n{answer[:2500]}\n\n"
        f"SOURCES:\n{listing}\n\n"
        'Return JSON only: {"verdict":"supported|partial|unsupported","note":"<short reason>"}'
    )
    try:
        raw = await asyncio.wait_for(
            generate_once(prompt, temperature=0.0, max_tokens=60),
            timeout=4.0,
        )
        match = _re.search(r"\{.*\}", raw or "", flags=_re.DOTALL)
        if not match:
            return "skip", ""
        data = json.loads(match.group(0))
        verdict = (data.get("verdict") or "").strip().lower()
        note = (data.get("note") or "").strip()
        if verdict in ("supported", "partial", "unsupported"):
            return verdict, note
        return "skip", ""
    except Exception:
        return "skip", ""


async def grade_relevance(
    query: str,
    items: list[dict],
    contexts: list[str],
    *,
    good_threshold: float,
    bad_threshold: float,
    enable_llm_grader: bool,
    llm_pass: float,
) -> GradeResult:
    """
    Decide whether the retrieval is good enough to ground an answer.

    - empty contexts                       -> "empty"  (force correction / fallback)
    - heuristic >= good_threshold          -> "good"   (cheap accept, no LLM)
    - borderline + LLM grader enabled      -> ask the model
    - otherwise                            -> "weak"   (trigger a corrective rewrite)
    """
    if not contexts:
        return GradeResult(0.0, "empty", "none", "no contexts retrieved")

    heuristic = estimate_retrieval_quality(query, items, top_n=3)

    if heuristic >= good_threshold:
        return GradeResult(
            heuristic, "good", "heuristic",
            f"overlap {heuristic:.2f} >= good {good_threshold:.2f}",
        )

    if enable_llm_grader and heuristic >= bad_threshold:
        llm_score = await _llm_grade(query, contexts)
        if llm_score is not None:
            verdict = "good" if llm_score >= llm_pass else "weak"
            return GradeResult(llm_score, verdict, "llm", f"llm relevance {llm_score:.2f}")

    return GradeResult(
        heuristic, "weak", "heuristic",
        f"overlap {heuristic:.2f} < good {good_threshold:.2f}",
    )
