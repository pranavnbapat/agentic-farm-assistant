# app/services/context_service.py
#
# Copied from farm_assistant/app/services/context_service.py (unchanged logic).
# Chunk ranking, parent collapsing, source tracking, and the two retrieval-quality
# helpers the agent's grader reuses: estimate_retrieval_quality + filter_items_by_min_score.

import logging, re

import re as _re

from typing import Dict, Any, List

from app.schemas import SourceItem
from app.config import get_settings

logger = logging.getLogger("agentic-fa.context")
S = get_settings()


def split_paragraphs(text: str) -> list[str]:
    parts = re.split(r'\n{2,}|(?<=[\.\?\!])\s+\n?', text)
    clean = [re.sub(r'\s+', ' ', p).strip() for p in parts]
    return [p for p in clean if len(p) > 40]


def rank_paragraphs(
    paragraphs: list[str],
    question: str,
    boost_terms: set[str] | None = None,
) -> list[tuple[int, str]]:
    q_tokens = {t for t in re.findall(r"[a-zA-Z]+", question.lower()) if len(t) > 2}
    bt = boost_terms or set()

    ranked: list[tuple[int, str]] = []
    for idx, p in enumerate(paragraphs):
        p_tokens = {t for t in re.findall(r"[a-zA-Z]+", p.lower()) if len(t) > 2}
        overlap = len(q_tokens & p_tokens)
        boost_overlap = len(p_tokens & bt)
        score = overlap * 10 + boost_overlap * 4 + max(0, 5 - idx)
        ranked.append((score, p))

    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked


def build_context_and_sources(
    items: List[Dict[str, Any]],
    question: str,
    top_k: int,
    max_context_chars: int,
) -> tuple[list[str], list[SourceItem]]:
    contexts: List[str] = []
    sources: List[SourceItem] = []
    total_chars = 0
    parent_to_idx: Dict[str, int] = {}
    PER_PARENT_CHAR_CAP = 3500

    def norm(v):
        if isinstance(v, list):
            return " ".join(map(str, v))
        return "" if v is None else str(v)

    for i, it in enumerate(items):
        if top_k > 0 and len(contexts) >= top_k:
            break

        src = it.get("_source", {}) if isinstance(it, dict) and "_source" in it else it
        _id = it.get("_id") if isinstance(it, dict) else None
        _score = it.get("_score") if isinstance(it, dict) else None
        title = (src.get("title") or "").strip()
        url = src.get("@id")
        subtitle = (src.get("subtitle") or "").strip()
        desc = (src.get("description") or "").strip()
        proj = (src.get("project_display_name") or src.get("projectDisplayName") or src.get("project_name") or src.get("projectName") or "")
        acronym = (src.get("project_acronym") or src.get("projectAcronym") or "")
        ptype = (src.get("project_type") or "")
        license_ = (src.get("license") or "")
        keywords = src.get("keywords") or []
        topics = src.get("topics") or []
        themes = src.get("themes") or []
        langs = src.get("languages") or []
        creators = src.get("creators") or []
        datec = (src.get("date_of_completion") or "")
        nice_url = (src.get("project_url") or src.get("projectUrl") or url or None)

        proj_str = ""
        if proj or acronym:
            cap = f"{proj}".strip() or f"{acronym}".strip()
            if ptype:
                proj_str = f"{cap} ({ptype})"
            else:
                proj_str = cap

        parent_key = (
            (src.get("parent_id") or "").strip()
            or (src.get("_orig_id") or "").strip()
            or (url or "").strip()
            or (title or "").strip().lower()
            or f"__row_{i}"
        )
        existing_idx = parent_to_idx.get(parent_key)

        if existing_idx is None:
            sid = f"S{len(sources) + 1}"
            sources.append(SourceItem(
                id=_id, url=url, display_url=nice_url,
                title=(title or proj_str or subtitle or None),
                score=_score,
                subtitle=subtitle or None,
                description=(desc[:300] if desc else None),
                project=(proj_str or None),
                license=(license_ or None),
                keywords=(keywords or None) if keywords else None,
                topics=(topics or None) if topics else None,
                themes=(themes or None) if themes else None,
                languages=(langs or None) if langs else None,
                creators=(creators or None) if creators else None,
                date_of_completion=(datec or None),
                sid=sid,
            ))
        else:
            sid = sources[existing_idx].sid or f"S{existing_idx + 1}"

        header_parts = [f"[{sid}]"]
        if title:
            header_parts.append(f"Title: {title}")
        if subtitle:
            header_parts.append(f"Subtitle: {subtitle}")
        if desc:
            header_parts.append(f"Description: {desc[:800]}")
        if proj_str or license_ or datec:
            meta_bits = []
            if proj_str:
                meta_bits.append(proj_str)
            if datec:
                meta_bits.append(f"Completed: {datec}")
            if license_:
                meta_bits.append(f"License: {license_}")
            header_parts.append(" · ".join(meta_bits))
        if keywords:
            header_parts.append("Keywords: " + ", ".join(keywords[:8]))
        if topics:
            header_parts.append("Topics: " + ", ".join(topics[:6]))
        header = "\n".join(header_parts).strip()

        llm_context = (src.get("llm_context") or "").strip() if isinstance(src.get("llm_context"), str) else ""
        flat_list = src.get("ko_content_flat")
        flat_text = ""
        if isinstance(flat_list, list):
            flat_text = " ".join(map(str, flat_list))
        elif isinstance(flat_list, str):
            flat_text = flat_list

        chosen_paras: list[str] = []
        if flat_text:
            paras = split_paragraphs(flat_text)
            boost_terms = set(t.lower() for t in (keywords or [])) | set(t.lower() for t in (topics or []))
            ranked = rank_paragraphs(paras, question=question or title, boost_terms=boost_terms)
            for _, p in ranked[:3]:
                if len(p) < 120:
                    continue
                chosen_paras.append(p[:800])
                if sum(len(x) for x in chosen_paras) > 1200:
                    break

        parts: list[str] = []
        if llm_context:
            chunk = llm_context[:2000]
        else:
            if header:
                parts.append(header)
            if chosen_paras:
                parts.append("Content:\n- " + "\n- ".join(chosen_paras))
            chunk = "\n".join(parts).strip() or (f"Title: {title}" if title else "")

        if chunk:
            chunk = chunk[:2000]
            if existing_idx is None:
                if total_chars + len(chunk) > max_context_chars:
                    sources.pop()
                    break
                contexts.append(chunk)
                parent_to_idx[parent_key] = len(contexts) - 1
                total_chars += len(chunk)
            else:
                merged = contexts[existing_idx]
                room_parent = max(0, PER_PARENT_CHAR_CAP - len(merged))
                room_global = max(0, max_context_chars - total_chars)
                room = min(room_parent, room_global)
                if room <= 0:
                    continue
                addition = chunk[:room]
                separator = "\n\n" if not merged.endswith("\n") else ""
                if len(separator) + len(addition) > room:
                    addition = addition[: max(0, room - len(separator))]
                if not addition:
                    continue
                contexts[existing_idx] = f"{merged}{separator}{addition}"
                total_chars += len(separator) + len(addition)
        elif existing_idx is None:
            sources.pop()

    logger.info(
        f"Extracted {len(contexts)} context chunk(s) over {len(sources)} unique source(s); "
        f"total_chars={total_chars}"
    )
    return contexts, sources


def _tokenise_alpha(text: str) -> list[str]:
    return [t.lower() for t in _re.findall(r"[A-Za-z]{3,}", text or "")]


def _overlap_ratio(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / max(1, min(len(a), len(b)))


def estimate_retrieval_quality(user_q: str, items: list[dict], top_n: int = 3) -> float:
    """Rough 0..1 estimate of how on-topic the retrieved items are (token overlap)."""
    qtok = set(_tokenise_alpha(user_q))
    if not qtok:
        return 0.0

    scores = []
    for it in items[:top_n]:
        src = it.get("_source", {}) if isinstance(it, dict) and "_source" in it else it
        title = (src.get("title") or "") + " " + (src.get("subtitle") or "")
        desc = (src.get("description") or "")
        sample = f"{title} {desc}"
        stok = set(_tokenise_alpha(sample))
        scores.append(_overlap_ratio(qtok, stok))

    if not scores:
        return 0.0
    s = sum(scores) / len(scores)
    return max(0.0, min(1.0, s))


def filter_items_by_min_score(items: list[dict], min_score: float) -> tuple[list[dict], dict[str, int | float]]:
    """Keep only retrieved items whose OpenSearch score clears a minimum bar."""
    if min_score <= 0:
        return items, {
            "input_count": len(items),
            "kept_count": len(items),
            "discarded_count": 0,
            "min_score_threshold": float(min_score),
        }

    kept: list[dict] = []
    discarded = 0
    for it in items:
        score = (it.get("_score") if isinstance(it, dict) else None)
        try:
            numeric_score = float(score)
        except (TypeError, ValueError):
            numeric_score = None
        if numeric_score is None or numeric_score < float(min_score):
            discarded += 1
            continue
        kept.append(it)

    return kept, {
        "input_count": len(items),
        "kept_count": len(kept),
        "discarded_count": discarded,
        "min_score_threshold": float(min_score),
    }
