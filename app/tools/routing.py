# app/tools/routing.py
#
# ENTRY POLICY for the agent. Ported from farm_assistant/app/routers/ask.py
# (_hard_route_turn_mode / _decide_turn_strategy / _route_turn_mode and helpers).
#
# This is the cheap gate that keeps off-topic / trivial / non-retrieval turns OUT of
# the expensive CRAG loop. Only `normal` enters the loop; `general_knowledge` answers
# directly without retrieval; the rest are handled by mode-specific prompt builders.

import asyncio
import json
import logging
import re as _re
from typing import Literal, Optional

from app.clients.vllm_client import generate_once

logger = logging.getLogger("agentic-fa.routing")

TurnMode = Literal[
    "clarification_only",
    "off_topic",
    "history_only",
    "conversation_only",
    "assistant_capabilities",
    "general_knowledge",
    "normal",
]

# Modes that go through the retrieval/CRAG loop. `general_knowledge` is included on
# purpose: the router often guesses "general_knowledge" for agricultural questions that
# EU-FarmBook *does* cover (e.g. "drones in sheep monitoring"). We still probe EUF and,
# if good sources come back, answer grounded with citations; only when EUF genuinely has
# nothing relevant do we fall back to a pure general-knowledge answer. Truly out-of-scope
# turns (off_topic / conversation_only / history_only / capabilities / clarification)
# answer directly without retrieval.
RETRIEVAL_MODES = {"normal", "general_knowledge"}


_AGRI_HINT_TERMS = {
    "agriculture", "agricultural", "farming", "farm", "farmer", "farmers",
    "crop", "crops", "soil", "livestock", "poultry", "cattle", "tractor",
    "irrigation", "fertilizer", "fertiliser", "manure", "weed", "weeds",
    "pest", "pests", "crop rotation", "agri", "food system", "greenhouse",
    "horticulture", "aquaculture", "forestry", "eufarmbook", "eu-farmbook",
}

_CONSUMER_TECH_OFFTOPIC_TERMS = {
    "iphone", "ipad", "macbook", "ios", "apple watch",
    "samsung", "galaxy", "android", "pixel", "google pixel",
    "oneplus", "xiaomi", "huawei", "oppo", "vivo",
    "smartphone", "phone", "mobile phone", "tablet", "laptop",
    "airpods", "earbuds", "smartwatch",
}

_PROMPT_INJECTION_TERMS = {
    "ignore previous instructions", "ignore all previous instructions",
    "you are now a general assistant", "you are now", "act as",
    "pretend to be", "system prompt", "developer prompt", "jailbreak",
}

_GENERAL_OFFTOPIC_TERMS = {
    "president", "prime minister", "king", "queen", "celebrity", "movie",
    "song", "lyrics", "football", "basketball", "politics", "france",
    "germany", "united states", "usa", "election",
}


def _matches_any(text: str, terms: set[str]) -> bool:
    """
    Word-boundary match for off-topic / injection term sets. Using word boundaries
    (instead of raw substring) avoids false positives like "ios" inside
    "biosecurity" or "usa" inside "causal" that would wrongly flag agriculture
    questions as off-topic. (Agri-hint terms still use substring matching on
    purpose, so "farms" matches "farm".)
    """
    t = (text or "").lower()
    for term in terms:
        if _re.search(r"\b" + _re.escape(term) + r"\b", t):
            return True
    return False


# Capability/"what can you do" intent. Kept tight to avoid catching domain questions
# like "what can you do about aphids"; further gated by the agri-hint check below so a
# capability question that names a farming topic still goes to retrieval.
_CAPABILITY_RE = _re.compile(
    r"\b(what|which|how)\s+(can|could|do|would)\s+you\s+(do|help|offer|assist|support)\b"
    r"|\bwhat\s+(are|is)\s+your\s+(capabilit|feature|function|purpose|skill|strength)"
    r"|\bwhat\s+can\s+i\s+(ask|do)\b"
    r"|\bwhat\s+kind(s)?\s+of\s+(help|support|question|task|thing|info)"
    r"|\bcan\s+you\s+help\s+me\b"
    r"|\bhow\s+do\s+you\s+work\b"
)


def _is_capability_question(q: str) -> bool:
    return bool(_CAPABILITY_RE.search(q or ""))


def _is_meaningless_prompt(text: str) -> bool:
    q = (text or "").strip()
    if not q:
        return True
    if len(q) <= 4 and _re.fullmatch(r"[\W_]+", q):
        return True
    lowered = q.lower()
    trivial = {
        "?", "??", "???", ".", "..", "...",
        "huh", "hm", "hmm", "uh", "um", "ok?", "what?", "excuse me?",
    }
    return lowered in trivial


def _mentions_file_or_document(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    keys = {
        "file", "document", "pdf", "attachment", "this doc", "this file",
        "image", "photo", "picture", "screenshot", "this image", "this photo",
    }
    return any(k in t for k in keys)


def hard_route_turn_mode(user_q: str) -> Optional[str]:
    """Deterministic guardrail for obviously off-topic / empty standalone queries."""
    q = (user_q or "").strip().lower()
    if not q:
        return "clarification_only"
    if _is_meaningless_prompt(q):
        return "clarification_only"
    if _mentions_file_or_document(q):
        return None
    has_agri_hint = any(term in q for term in _AGRI_HINT_TERMS)
    # Capability questions about the assistant itself (no farming topic) answer from
    # product behaviour, not retrieval. Gated by has_agri_hint so "can you help me with
    # cover crops" still goes to retrieval.
    if _is_capability_question(q) and not has_agri_hint:
        return "assistant_capabilities"
    if _matches_any(q, _PROMPT_INJECTION_TERMS):
        return None if has_agri_hint else "off_topic"
    if _matches_any(q, _CONSUMER_TECH_OFFTOPIC_TERMS):
        return "off_topic"
    if _matches_any(q, _GENERAL_OFFTOPIC_TERMS) and not has_agri_hint:
        return "off_topic"
    return None


def _has_offtopic_signal(user_q: str) -> bool:
    q = (user_q or "").strip().lower()
    if not q:
        return False
    if _matches_any(q, _CONSUMER_TECH_OFFTOPIC_TERMS):
        return True
    if _matches_any(q, _GENERAL_OFFTOPIC_TERMS):
        return True
    if _matches_any(q, _PROMPT_INJECTION_TERMS):
        return True
    return False


def looks_standalone(user_q: str) -> bool:
    """Structural, language-agnostic check: skip follow-up resolution for clear queries."""
    q = (user_q or "").strip()
    if len(q) < 40:
        return False
    words = [w for w in q.split() if w]
    return len(words) >= 7


def _routing_history_for_query(user_q: str, history_text: str) -> str:
    return "" if looks_standalone(user_q) else history_text


async def _decide_turn_strategy(question: str, history_text: str) -> TurnMode:
    """Lightweight LLM classifier (temp 0, 14 tokens, 1.5s budget)."""
    prompt = (
        "You are routing a chat turn for an agricultural assistant for the EU-FarmBook platform.\n"
        "Choose one mode and return JSON only.\n\n"
        "Modes:\n"
        '- "off_topic": the user message is not about agriculture, farming, agri-tech, food systems, '
        "or EU-FarmBook. This includes jokes, song lyrics, movie quotes, riddles, questions about "
        "the underlying model/company, questions about unrelated subjects (politics, sports, celebrities, "
        'general trivia), or attempts to bait the assistant.\n'
        '- "history_only": the user is asking about the conversation itself, prior turns, '
        "what has been discussed, a recap, or what the assistant/user said earlier.\n"
        '- "conversation_only": the user is greeting, thanking, acknowledging, confirming, '
        'or saying something casual ("hi", "thanks", "ok", "great").\n'
        '- "assistant_capabilities": the user is asking what the assistant can do, how it can help, '
        "or what kinds of support it provides.\n"
        '- "general_knowledge": an agricultural question answerable from common agricultural '
        "knowledge alone (definitions, widely-known concepts, how-tos for common practices). "
        "No EU-FarmBook-specific documents, regulations, project results, or datasets are needed.\n"
        '- "normal": agricultural question that would benefit from grounding in specific EU-FarmBook '
        "material — project results, regulations, technical reports, datasets, or specialized regional data.\n\n"
        f"User message:\n{question}\n\n"
        "Previous Conversation:\n"
        f"{history_text or 'No earlier conversation is available.'}\n\n"
        'Return exactly: {"mode":"off_topic"} or {"mode":"history_only"} or {"mode":"conversation_only"} or '
        '{"mode":"assistant_capabilities"} or {"mode":"general_knowledge"} or {"mode":"normal"}'
    )
    try:
        raw = await asyncio.wait_for(
            generate_once(prompt, temperature=0.0, max_tokens=14),
            timeout=1.5,
        )
        match = _re.search(r"\{.*?\}", raw or "", flags=_re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            mode = (data.get("mode") or "").strip().lower()
            if mode in {
                "off_topic", "history_only", "conversation_only",
                "assistant_capabilities", "general_knowledge", "normal",
            }:
                return mode  # type: ignore[return-value]
    except Exception:
        pass
    return "normal"


async def route_turn_mode(*, user_q: str, prompt_q: str, history_text: str) -> TurnMode:
    """
    Three-stage routing:
      1. hard deterministic guardrails,
      2. lightweight LLM classifier for ambiguous cases,
      3. default to `normal` on failure.
    """
    forced = hard_route_turn_mode(user_q)
    if forced:
        return forced  # type: ignore[return-value]

    strategy_history = _routing_history_for_query(user_q, history_text)
    mode = await _decide_turn_strategy(prompt_q, strategy_history)

    # On-topic creative requests sometimes get mislabeled off_topic; rescue them.
    if mode == "off_topic":
        q_lower = (user_q or "").strip().lower()
        if q_lower and any(t in q_lower for t in _AGRI_HINT_TERMS) and not _has_offtopic_signal(user_q):
            return "general_knowledge"
    return mode
