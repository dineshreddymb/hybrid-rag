"""Reranking candidate documents using Jina Reranker API.

Used after Reciprocal Rank Fusion (RRF) to further refine top document candidates.
"""
from __future__ import annotations

import httpx

from backend_app.config import get_settings
from backend_app.logging import get_logger

log = get_logger(__name__)


def rerank_documents(query: str, items: list[dict], top_n: int | None = None) -> list[dict]:
    """Rerank `items` (which have 'text' keys) against `query` using Jina Reranker API.

    Fails open: returns the original items if disabled or if the API call fails.
    """
    s = get_settings()
    if s.rerank_provider != "jina" or not s.jina_api_key or not items:
        return items[:top_n] if top_n else items

    texts = [item.get("text", "") or " " for item in items]
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                s.jina_rerank_url,
                headers={
                    "Authorization": f"Bearer {s.jina_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": s.rerank_model,
                    "query": query,
                    "top_n": top_n or len(items),
                    "documents": texts,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            reranked = []
            for rank, r in enumerate(results, start=1):
                idx = r.get("index")
                if idx is not None and 0 <= idx < len(items):
                    orig = dict(items[idx])
                    orig["rerank_score"] = float(r.get("relevance_score", 0.0))
                    orig["rank"] = rank
                    reranked.append(orig)
            return reranked if reranked else (items[:top_n] if top_n else items)
    except Exception as exc:
        log.warning("rerank_failed", error=str(exc))
        return items[:top_n] if top_n else items
