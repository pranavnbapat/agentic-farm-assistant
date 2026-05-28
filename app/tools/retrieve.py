# app/tools/retrieve.py
#
# RETRIEVE tool. Composes the reused services:
#   search_service.build_search_payload + collect_os_items  (OpenSearch /llm_retrieve)
#   context_service.filter_items_by_min_score               (hard score floor)
#   context_service.build_context_and_sources               (rank + parent-collapse)
#
# Accepts one or more queries (the planner may pass sub-queries); pools and
# de-duplicates hits, then builds a single grounded context set.

import logging
from dataclasses import dataclass, field
from typing import Any

from app.clients.opensearch_client import os_async_client, os_headers, os_auth
from app.schemas import AskIn, SourceItem
from app.services.search_service import build_search_payload, collect_os_items
from app.services.context_service import build_context_and_sources, filter_items_by_min_score

logger = logging.getLogger("agentic-fa.retrieve")


@dataclass
class RetrievalResult:
    queries: list[str]
    items: list[dict[str, Any]]          # after the min-score filter
    raw_count: int                       # pooled hits before the min-score filter
    contexts: list[str] = field(default_factory=list)
    sources: list[SourceItem] = field(default_factory=list)


async def retrieve(
    queries: list[str],
    *,
    rank_question: str,
    top_k: int,
    max_context_chars: int,
    min_score: float,
    timeout: float = 30.0,
) -> RetrievalResult:
    pooled: list[dict[str, Any]] = []
    seen_ids: set = set()

    async with os_async_client(timeout=timeout) as client:
        headers = os_headers()
        auth = os_auth()
        for q in queries:
            if not (q or "").strip():
                continue
            payload = build_search_payload(AskIn(question=q, top_k=top_k))
            items = await collect_os_items(client, payload, [1], headers, auth)
            for it in items:
                _id = (it.get("_id") if isinstance(it, dict) else None) or id(it)
                if _id in seen_ids:
                    continue
                seen_ids.add(_id)
                pooled.append(it)

    raw_count = len(pooled)
    filtered, stats = filter_items_by_min_score(pooled, min_score=min_score)
    if stats["discarded_count"] > 0:
        logger.info(
            "Score-filtered items: kept=%s discarded=%s threshold=%.3f",
            stats["kept_count"], stats["discarded_count"], stats["min_score_threshold"],
        )

    contexts, sources = build_context_and_sources(
        items=filtered,
        question=rank_question,
        top_k=top_k,
        max_context_chars=max_context_chars,
    )
    return RetrievalResult(
        queries=list(queries),
        items=filtered,
        raw_count=raw_count,
        contexts=contexts,
        sources=sources,
    )
