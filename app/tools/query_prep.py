# app/tools/query_prep.py
#
# Query-shaping tools. The first two are ported from farm_assistant
# (_resolve_turn_context, _normalize_query_for_retrieval). `rewrite_query` and
# `decompose_question` are the NEW agent actions the CRAG loop calls mid-flight.

import asyncio
import json
import logging
import re as _re

from app.clients.vllm_client import generate_once

logger = logging.getLogger("agentic-fa.query_prep")


def should_skip_query_normalization(text: str) -> bool:
    # Avoid accidental translation for non-ASCII queries (e.g. Greek).
    return any(ord(ch) > 127 for ch in (text or ""))


async def resolve_turn_context(
    question: str,
    history_text: str,
    last_assistant_question: str = "",
    followup_hint: str = "",
) -> dict:
    """Turn terse/elliptical replies into a standalone interpretation (ported)."""
    prompt = (
        "You are interpreting the user's latest turn for an agricultural assistant.\n"
        "Return JSON only.\n\n"
        "Your task:\n"
        "1. Decide whether the latest user message is already a standalone request.\n"
        "2. If it is short, elliptical, or mainly confirms the assistant's previous question, "
        "rewrite it into a clear standalone intent grounded only in the conversation.\n"
        "3. Provide a prompt-ready instruction that helps the assistant answer the turn cleanly.\n\n"
        "Rules:\n"
        "- Do not invent facts not present in the conversation.\n"
        "- Do not continue or quote trailing fragments from the previous assistant answer.\n"
        "- If the user is agreeing to proceed after a prior assistant question, make that explicit.\n"
        "- Keep the resolved text concise and faithful.\n\n"
        f"Latest user message:\n{question}\n\n"
        f"Last assistant question:\n{last_assistant_question or 'None'}\n\n"
        f"Follow-up hint:\n{followup_hint or 'None'}\n\n"
        "Previous Conversation:\n"
        f"{history_text or 'No earlier conversation is available.'}\n\n"
        "Return exactly this JSON shape:\n"
        '{"resolved_user_message":"...","assistant_instruction":"..."}'
    )
    try:
        raw = await asyncio.wait_for(
            generate_once(prompt, temperature=0.0, max_tokens=180),
            timeout=2.0,
        )
        match = _re.search(r"\{.*\}", raw or "", flags=_re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            resolved = (data.get("resolved_user_message") or "").strip()
            instruction = (data.get("assistant_instruction") or "").strip()
            if resolved or instruction:
                return {
                    "resolved_user_message": resolved or question,
                    "assistant_instruction": instruction or resolved or question,
                }
    except Exception:
        pass
    return {"resolved_user_message": question, "assistant_instruction": question}


async def normalize_query_for_retrieval(text: str) -> str:
    """Best-effort spelling/grammar cleanup for retrieval (ported)."""
    raw = (text or "").strip()
    if not raw:
        return raw
    prompt = (
        "Rewrite the user query with corrected spelling/grammar, preserving exact intent. "
        "Keep it concise, one line, no explanation.\n\n"
        f"Query: {raw}\n\n"
        "Rewritten query:"
    )
    try:
        rewritten = await asyncio.wait_for(
            generate_once(prompt, temperature=0.0, max_tokens=48),
            timeout=1.2,
        )
    except Exception:
        return raw
    line = (rewritten or "").strip().splitlines()
    if not line:
        return raw
    cleaned = line[0].strip(" \"'")
    return cleaned or raw


# --------------------------------------------------------------------------- #
# NEW agent actions
# --------------------------------------------------------------------------- #

async def rewrite_query(query: str, reason: str = "") -> str:
    """
    CRAG corrective action: produce an alternative search query when the previous
    retrieval was weak. Broadens/rephrases with domain synonyms while preserving intent.
    Falls back to the original query on any error/timeout.
    """
    raw = (query or "").strip()
    if not raw:
        return raw
    prompt = (
        "You are improving a search query for an agricultural knowledge base "
        "(EU-FarmBook). The previous search returned weak or off-target results.\n"
        f"Reason the previous results were weak: {reason or 'low relevance'}\n\n"
        "Rewrite the query to retrieve better documents: keep the same intent, but "
        "rephrase using clearer domain terminology and likely synonyms, and drop "
        "filler words. Return ONE line, just the rewritten query, no explanation.\n\n"
        f"Original query: {raw}\n\n"
        "Improved query:"
    )
    try:
        rewritten = await asyncio.wait_for(
            generate_once(prompt, temperature=0.2, max_tokens=48),
            timeout=2.0,
        )
    except Exception:
        return raw
    line = (rewritten or "").strip().splitlines()
    if not line:
        return raw
    cleaned = line[0].strip(" \"'")
    # Guard against a no-op or degenerate rewrite.
    if not cleaned or cleaned.lower() == raw.lower():
        return raw
    return cleaned


async def decompose_question(question: str, max_subqueries: int = 3) -> list[str]:
    """
    Planner action: split a multi-part question into independent sub-queries.
    Returns [question] when the question is atomic (the common case).
    """
    raw = (question or "").strip()
    if not raw:
        return [raw]
    prompt = (
        "Split the following agricultural question into the minimum set of independent "
        "search sub-questions needed to answer it fully. If it is already a single "
        "question, return just that one.\n"
        f"Return a JSON array of at most {max_subqueries} short strings, nothing else.\n\n"
        f"Question: {raw}\n\n"
        "JSON array:"
    )
    try:
        raw_out = await asyncio.wait_for(
            generate_once(prompt, temperature=0.0, max_tokens=120),
            timeout=2.5,
        )
    except Exception:
        return [raw]

    start = (raw_out or "").find("[")
    end = (raw_out or "").rfind("]")
    if start == -1 or end == -1 or end <= start:
        return [raw]
    try:
        parsed = json.loads(raw_out[start : end + 1])
    except Exception:
        return [raw]
    if not isinstance(parsed, list):
        return [raw]

    subs: list[str] = []
    for item in parsed:
        if isinstance(item, str) and item.strip():
            subs.append(item.strip()[:300])
        if len(subs) >= max_subqueries:
            break
    return subs or [raw]
