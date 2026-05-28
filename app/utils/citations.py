# app/utils/citations.py
#
# Citation post-processing, extracted from farm_assistant/app/routers/ask.py.
# Normalises citation forms ([S1], (source: 1), ranges), strips citation numbers
# that don't map to a real source, and returns the cited-source subset.

import re as _re
from typing import Any


def sanitize_generated_markdown(text: str) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _normalize_citation_forms(text: str) -> str:
    norm = _re.sub(r"\(\s*source[s]?:?\s*\[?\s*(?:S)?(\d+)\s*\]?\s*\)", r"[\1]", text, flags=_re.IGNORECASE)
    norm = _re.sub(r"\bsource[s]?:?\s*\[?\s*(?:S)?(\d+)\s*\]?\b", r"[\1]", norm, flags=_re.IGNORECASE)
    norm = _re.sub(r"\[\s*[sS](\d+)\s*\]", r"[\1]", norm)
    norm = _re.sub(r"\(\s*[sS](\d+)\s*\)", r"[\1]", norm)
    norm = _re.sub(
        r"\[\s*([sS]\d+(?:\s*[,–-]\s*[sS]?\d+)*)\s*\]",
        lambda m: "[" + _re.sub(r"[sS]", "", m.group(1)) + "]",
        norm,
    )
    return norm


def extract_cited_numbers(text: str) -> set[int]:
    cited_nums: set[int] = set()
    for match in _re.finditer(r"\[\s*(\d+)\s*\]", text):
        cited_nums.add(int(match.group(1)))
    for match in _re.finditer(r"\[\s*([\d\s,–-]+)\s*\]", text):
        for token in _re.split(r"[,\s–-]+", match.group(1)):
            if token.isdigit():
                cited_nums.add(int(token))
    return cited_nums


def strip_orphan_citations(text: str, valid_source_numbers: set[int]) -> str:
    if not valid_source_numbers:
        return _re.sub(r"\s*\[\s*[\d\s,–-]+\s*\]", "", text)

    def _replace(match):
        tokens = [
            int(token)
            for token in _re.split(r"[,\s–-]+", match.group(1))
            if token.isdigit()
        ]
        valid_tokens = [token for token in tokens if token in valid_source_numbers]
        if not valid_tokens:
            return ""
        return "[" + ", ".join(str(token) for token in valid_tokens) + "]"

    cleaned = _re.sub(r"\[\s*([\d\s,–-]+)\s*\]", _replace, text)
    cleaned = _re.sub(r" +([.,;:!?])", r"\1", cleaned)
    return cleaned


def finalize_citations(full_text: str, all_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Given the streamed answer text and the full positional source list, return the
    subset of sources actually cited in the answer (orphans removed).
    """
    norm_text = _normalize_citation_forms(full_text)
    valid_source_numbers = {s["n"] for s in all_sources}
    norm_text = strip_orphan_citations(norm_text, valid_source_numbers)
    cited_nums = extract_cited_numbers(norm_text)
    if not cited_nums:
        return []
    by_num = {s["n"]: s for s in all_sources}
    return [by_num[n] for n in sorted(cited_nums) if n in by_num]


def sources_to_payload(sources: list) -> list[dict[str, Any]]:
    """Positional source list (n=1..N) for citation mapping and the SSE 'sources' event."""
    return [
        {
            "n": i + 1,
            "sid": getattr(s, "sid", None),
            "id": getattr(s, "id", None),
            "title": getattr(s, "title", None),
            "project": getattr(s, "project", None),
            "url": getattr(s, "url", None),
            "display_url": getattr(s, "display_url", None),
            "license": getattr(s, "license", None),
        }
        for i, s in enumerate(sources)
    ]
