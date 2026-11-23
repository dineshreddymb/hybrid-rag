"""Hybrid search = dense (Qdrant) + BM25, fused with RRF. Used by /search and the RAG graph."""
from __future__ import annotations

import time
from typing import Literal

from backend_app.cache import store as cache
from backend_app.config import get_settings
from backend_app.db import repositories as repo
from backend_app.embeddings import get_embedder
from backend_app.retrieval import bm25
from backend_app.retrieval.fusion import rrf
from backend_app.vectorstore import qdrant_store

Mode = Literal["hybrid", "dense", "bm25"]


def _embedding_id() -> list[str]:
    s = get_settings()
    return [s.embedding_provider, s.embedding_model, s.rerank_provider, s.rerank_model]


def dense_search(query: str, top_k: int) -> list[dict]:
    """Query vector (cached embedding) -> Qdrant. The result list is cached for 10 minutes."""
    key = None
    if cache.enabled():
        key = cache.versioned_key({"k": "dense", "q": query, "top_k": top_k,
                                   "emb": _embedding_id()})
        hit = cache.get_json("search", key)
        if hit is not None:
            return hit
    result = qdrant_store.search(get_embedder().embed_query(query), top_k)
    if key:
        cache.set_json("search", key, result)
    return result


def bm25_search(query: str, top_k: int) -> list[dict]:
    key = None
    if cache.enabled():
        key = cache.versioned_key({"k": "bm25", "q": query, "top_k": top_k})
        hit = cache.get_json("search", key)
        if hit is not None:
            return hit
    result = bm25.search(query, top_k)
    if key:
        cache.set_json("search", key, result)
    return result


def fuse(mode: Mode, dense: list[dict], kw: list[dict], weight_dense: float,
         top_k: int) -> list[dict]:
    s = get_settings()
    if mode == "dense":
        return rrf(dense, [], 1.0, s.rrf_k, top_k)
    if mode == "bm25":
        return rrf([], kw, 0.0, s.rrf_k, top_k)
    return rrf(dense, kw, weight_dense, s.rrf_k, top_k)


def attach_text(items: list[dict]) -> list[dict]:
    """Add chunk text (from Mongo) to result rows for display / prompting."""
    by_id = repo.get_chunks_by_ids([i["chunk_id"] for i in items])
    out = []
    for i in items:
        c = by_id.get(i["chunk_id"], {})
        out.append({**i, "text": c.get("text", ""), "source": c.get("source", i.get("source")),
                    "heading_path": c.get("heading_path", i.get("heading_path", ""))})
    return out


def hybrid_search(query: str, mode: Mode = "hybrid", top_k: int | None = None,
                  weight_dense: float | None = None, top_k_dense: int | None = None,
                  top_k_bm25: int | None = None, use_cache: bool = True) -> dict:
    token = cache.use_cache_var.set(use_cache)
    try:
        return _hybrid_search(query, mode, top_k, weight_dense, top_k_dense, top_k_bm25)
    finally:
        cache.use_cache_var.reset(token)


def _hybrid_search(query: str, mode: Mode, top_k: int | None, weight_dense: float | None,
                   top_k_dense: int | None, top_k_bm25: int | None) -> dict:
    s = get_settings()
    top_k = top_k or s.top_k_final
    weight = s.hybrid_weight_dense if weight_dense is None else weight_dense
    timings = {}

    key = None
    if cache.enabled():
        key = cache.versioned_key({"k": "search", "q": query, "mode": mode, "top_k": top_k,
                                   "w": weight, "kd": top_k_dense or s.top_k_dense,
                                   "kb": top_k_bm25 or s.top_k_bm25, "emb": _embedding_id()})
        hit = cache.get_json("search", key)
        if hit is not None:
            return {**hit, "cached": True, "timings": {"dense_ms": 0.0, "bm25_ms": 0.0}}

    t0 = time.perf_counter()
    dense = dense_search(query, top_k_dense or s.top_k_dense) if mode in ("hybrid", "dense") else []
    timings["dense_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    t0 = time.perf_counter()
    kw = bm25_search(query, top_k_bm25 or s.top_k_bm25) if mode in ("hybrid", "bm25") else []
    timings["bm25_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    pool_k = top_k * 2 if s.rerank_provider == "jina" and s.jina_api_key else top_k
    fused_candidates = attach_text(fuse(mode, dense, kw, weight, pool_k))

    if s.rerank_provider == "jina" and s.jina_api_key:
        from backend_app.retrieval.reranker import rerank_documents
        t0 = time.perf_counter()
        final_fused = rerank_documents(query, fused_candidates, top_n=top_k)
        timings["rerank_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    else:
        final_fused = fused_candidates[:top_k]

    result = {
        "query": query, "mode": mode, "weight_dense": weight, "top_k": top_k,
        "dense": attach_text(dense), "bm25": attach_text(kw), "fused": final_fused,
        "timings": timings, "cached": False,
    }
    if key:
        cache.set_json("search", key, result)
    return result
