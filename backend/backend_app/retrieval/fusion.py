"""Weighted Reciprocal Rank Fusion.

    score(chunk) = w_dense / (k + rank_dense) + w_bm25 / (k + rank_bm25)

Ranks start at 1; a chunk missing from one list gets nothing from that list.
RRF only uses ranks, so it does not matter that cosine scores (0..1) and BM25 scores
(0..~20) live on different scales.
"""
from __future__ import annotations


def rrf(dense: list[dict], bm25: list[dict], weight_dense: float = 0.5, k: int = 60,
        top_k: int | None = None) -> list[dict]:
    w_d, w_b = weight_dense, 1.0 - weight_dense
    fused: dict[str, dict] = {}
    for rank, item in enumerate(dense, start=1):
        e = fused.setdefault(item["chunk_id"], {**item, "dense_rank": None, "bm25_rank": None,
                                                "score": 0.0})
        e["dense_rank"], e["dense_score"] = rank, item["score"]
        e["score"] += w_d / (k + rank)
    for rank, item in enumerate(bm25, start=1):
        e = fused.setdefault(item["chunk_id"], {**item, "dense_rank": None, "bm25_rank": None,
                                                "score": 0.0})
        e["bm25_rank"], e["bm25_score"] = rank, item["score"]
        e["score"] += w_b / (k + rank)
    ranked = sorted(fused.values(), key=lambda e: e["score"], reverse=True)
    for i, e in enumerate(ranked, start=1):
        e["rank"] = i
    return ranked[:top_k] if top_k else ranked
