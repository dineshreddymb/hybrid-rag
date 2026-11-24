from typing import TypedDict


class RAGState(TypedDict, total=False):
    # input
    question: str
    history: list[dict]          # [{"role": "user"|"assistant", "content": str}]
    mode: str                    # hybrid | dense | bm25
    top_k: int
    weight_dense: float
    fast_mode: bool
    # working state
    query: str                   # current search query (rewritten)
    attempt: int                 # how many times rewrite_query ran this turn
    dense: list[dict]
    bm25: list[dict]
    fused: list[dict]
    relevant: list[dict]
    # output
    answer: str
    citations: list[dict]
    grounded: bool | None
    grounding_reason: str
    not_found: bool
