"""BM25 keyword search over all chunks (rank_bm25 Okapi BM25), kept in memory.

The index is rebuilt from MongoDB at startup and after every ingestion. With ~100 chunks
this takes milliseconds.

Tokenizer: lowercase, and keep identifiers such as `e-217`, `4.2.1`, `irrigation.max_cycle_sec`
as whole tokens *and* their parts, so "E-217" matches exactly while "max_cycle_sec" still
matches "irrigation.max_cycle_sec".
"""
from __future__ import annotations

import re
import threading

from rank_bm25 import BM25Okapi

from backend_app.db import repositories as repo
from backend_app.logging import get_logger

log = get_logger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+(?:[._\-/][a-z0-9]+)*")
_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how",
    "i", "if", "in", "is", "it", "my", "of", "on", "or", "the", "to", "was", "what", "when",
    "which", "who", "why", "with", "you", "your", "we", "our", "this", "that", "there",
}


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for tok in _TOKEN.findall(text.lower()):
        if tok in _STOP:
            continue
        tokens.append(tok)
        # level 1: split on "." and "/" (irrigation.max_cycle_sec -> max_cycle_sec)
        segments = [p for p in re.split(r"[./]", tok) if p]
        if len(segments) > 1:
            tokens.extend(p for p in segments if p not in _STOP and not p.isdigit())
        # level 2: split on "-" and "_" too (max_cycle_sec -> max, cycle, sec)
        parts = [p for p in re.split(r"[._\-/]", tok) if p]
        if len(parts) > 1:
            tokens.extend(p for p in parts if p not in _STOP)
    return tokens


class BM25Index:
    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        corpus = [tokenize(c.get("index_text") or c["text"]) for c in chunks]
        self.bm25 = BM25Okapi(corpus) if corpus else None

    def search(self, query: str, top_k: int) -> list[dict]:
        if not self.bm25:
            return []
        q = tokenize(query)
        if not q:
            return []
        scores = self.bm25.get_scores(q)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out = []
        for i in ranked[:top_k]:
            if scores[i] <= 0:
                break
            c = self.chunks[i]
            out.append({"chunk_id": c["chunk_id"], "score": float(scores[i]),
                        "doc_id": c["doc_id"], "source": c["source"],
                        "heading_path": c.get("heading_path", ""), "position": c["position"]})
        return out


_index: BM25Index = BM25Index([])
_lock = threading.Lock()


def rebuild() -> int:
    global _index
    chunks = repo.all_chunks()
    new_index = BM25Index(chunks)
    with _lock:
        _index = new_index
    log.info("bm25_rebuilt", chunks=len(chunks))
    return len(chunks)


def search(query: str, top_k: int) -> list[dict]:
    with _lock:
        idx = _index
    return idx.search(query, top_k)


def size() -> int:
    return len(_index.chunks)
