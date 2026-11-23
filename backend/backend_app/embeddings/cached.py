"""Redis cache in front of any embedder: the same text with the same provider + model always
gives the same vector, so a vector is computed (and paid for) once.

Key: emb:{provider}:{model}:{sha256(text)}  - the provider is part of the key so offline `fake`
vectors can never be mistaken for real `hf_api` ones. Queries use a separate key namespace
because the embedder adds the bge query prefix to them.
"""
from __future__ import annotations

import hashlib

from langchain_core.embeddings import Embeddings

from backend_app.cache import store


class CachedEmbeddings(Embeddings):
    def __init__(self, inner: Embeddings, provider: str, model: str):
        self.inner = inner
        self.prefix = f"{provider}:{model}:"

    def _key(self, kind: str, text: str) -> str:
        return self.prefix + hashlib.sha256(f"{kind}\n{text}".encode()).hexdigest()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "doc", self.inner.embed_documents)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query", lambda ts: [self.inner.embed_query(ts[0])])[0]

    def _embed(self, texts: list[str], kind: str, compute) -> list[list[float]]:
        if not store.enabled() or not texts:
            return compute(texts)
        keys = [self._key(kind, t) for t in texts]
        cached = store.get_many("embedding", keys)
        result: list[list[float] | None] = [store.bytes_to_vec(c) if c is not None else None
                                            for c in cached]
        missing = [i for i, v in enumerate(result) if v is None]
        if missing:
            fresh = compute([texts[i] for i in missing])
            to_store = {}
            for i, vec in zip(missing, fresh):
                result[i] = vec
                to_store[keys[i]] = store.vec_to_bytes(vec)
            store.set_many("embedding", to_store)
        return result  # type: ignore[return-value]
