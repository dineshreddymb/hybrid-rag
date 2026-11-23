"""Embedding providers. All implement LangChain's `Embeddings` interface, so the same
object is used by ingestion, retrieval and RAGAS.

- hf_api : Hugging Face Inference API (needs HF_TOKEN) — the default
- tei    : a local text-embeddings-inference container (optional offline mode)
- fake   : deterministic hash vectors, no network (tests / trying things out)
"""
from __future__ import annotations

import hashlib
import math
import time
from functools import lru_cache

import httpx
from langchain_core.embeddings import Embeddings

from backend_app.config import Settings, get_settings
from backend_app.logging import get_logger

log = get_logger(__name__)


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _pool(item) -> list[float]:
    """HF returns either one vector per text or one vector per token; mean-pool the latter."""
    if item and isinstance(item[0], list):
        dim = len(item[0])
        return [sum(tok[i] for tok in item) / len(item) for i in range(dim)]
    return item


class _HTTPEmbeddings(Embeddings):
    def __init__(self, settings: Settings):
        self.s = settings
        self._http = httpx.Client(timeout=60.0)

    # subclasses implement _request(texts) -> list[list[float]]
    def _request(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError

    def _with_retry(self, texts: list[str]) -> list[list[float]]:
        delay = 2.0
        for attempt in range(1, 6):
            try:
                return self._request(texts)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                # 429 = rate limited, 503 = model loading; both are worth waiting for.
                if code in (429, 500, 502, 503, 504) and attempt < 5:
                    wait = float(exc.response.headers.get("retry-after", delay))
                    log.warning("embedding_retry", status=code, attempt=attempt, wait_s=wait)
                    time.sleep(min(wait, 30))
                    delay *= 2
                    continue
                raise RuntimeError(f"Embedding API error {code}: {exc.response.text[:300]}") from exc
            except httpx.TransportError as exc:
                if attempt < 5:
                    log.warning("embedding_retry", error=str(exc), attempt=attempt)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError("unreachable")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        bs = max(1, self.s.embedding_batch_size)
        for i in range(0, len(texts), bs):
            batch = texts[i:i + bs]
            vectors = [_normalize(_pool(v)) for v in self._with_retry(batch)]
            if vectors and len(vectors[0]) != self.s.embedding_dim:
                raise RuntimeError(
                    f"Model returned {len(vectors[0])}-dim vectors but EMBEDDING_DIM="
                    f"{self.s.embedding_dim}. Fix EMBEDDING_DIM or the model name.")
            out.extend(vectors)
        return out

    def embed_query(self, text: str) -> list[float]:
        prefix = self.s.embedding_query_prefix if "bge" in self.s.embedding_model.lower() else ""
        return self.embed_documents([prefix + text])[0]


class HFInferenceEmbeddings(_HTTPEmbeddings):
    def _request(self, texts: list[str]) -> list[list[float]]:
        if not self.s.hf_token:
            raise RuntimeError("HF_TOKEN is not set")
        url = self.s.hf_api_url.format(model=self.s.embedding_model)
        r = self._http.post(url, headers={"Authorization": f"Bearer {self.s.hf_token}"},
                            json={"inputs": texts, "options": {"wait_for_model": True}})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"HF API: {data['error']}")
        return data


class TEIEmbeddings(_HTTPEmbeddings):
    def _request(self, texts: list[str]) -> list[list[float]]:
        r = self._http.post(f"{self.s.tei_url.rstrip('/')}/embed", json={"inputs": texts})
        r.raise_for_status()
        return r.json()


class JinaEmbeddings(_HTTPEmbeddings):
    def _request_batch(self, texts: list[str], task: str) -> list[list[float]]:
        if not self.s.jina_api_key:
            raise RuntimeError("JINA_API_KEY is not set (add JINA_API_KEY to .env)")
        payload = {
            "model": self.s.embedding_model,
            "task": task,
            "dimensions": self.s.embedding_dim,
            "normalized": True,
            "embedding_type": "float",
            "input": texts,
        }
        r = self._http.post(
            self.s.jina_embedding_url,
            headers={
                "Authorization": f"Bearer {self.s.jina_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            items = sorted(data["data"], key=lambda x: x.get("index", 0))
            return [it["embedding"] for it in items]
        if isinstance(data, dict) and "message" in data:
            raise RuntimeError(f"Jina API: {data['message']}")
        raise RuntimeError(f"Jina API unexpected response: {data}")

    def _with_retry_jina(self, texts: list[str], task: str) -> list[list[float]]:
        delay = 2.0
        for attempt in range(1, 6):
            try:
                return self._request_batch(texts, task)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code in (429, 500, 502, 503, 504) and attempt < 5:
                    wait = float(exc.response.headers.get("retry-after", delay))
                    log.warning("embedding_retry", status=code, attempt=attempt, wait_s=wait)
                    time.sleep(min(wait, 30))
                    delay *= 2
                    continue
                raise RuntimeError(f"Embedding API error {code}: {exc.response.text[:300]}") from exc
            except httpx.TransportError as exc:
                if attempt < 5:
                    log.warning("embedding_retry", error=str(exc), attempt=attempt)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError("unreachable")

    def _request(self, texts: list[str]) -> list[list[float]]:
        return self._request_batch(texts, "retrieval.passage")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        bs = max(1, self.s.embedding_batch_size)
        for i in range(0, len(texts), bs):
            batch = texts[i:i + bs]
            vectors = [_normalize(v) for v in self._with_retry_jina(batch, "retrieval.passage")]
            if vectors and len(vectors[0]) != self.s.embedding_dim:
                raise RuntimeError(
                    f"Model returned {len(vectors[0])}-dim vectors but EMBEDDING_DIM="
                    f"{self.s.embedding_dim}. Fix EMBEDDING_DIM or the model name."
                )
            out.extend(vectors)
        return out

    def embed_query(self, text: str) -> list[float]:
        vectors = self._with_retry_jina([text], "retrieval.query")
        return _normalize(vectors[0])


class FakeEmbeddings(Embeddings):
    """Deterministic bag-of-words hash vectors: similar wording -> similar vectors.
    Good enough to exercise the whole pipeline without any API key."""

    def __init__(self, dim: int):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for word in text.lower().split():
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 8) % 2 else -1.0
        return _normalize(v)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@lru_cache
def get_embedder(cached: bool = True) -> Embeddings:
    """The embedder used everywhere. `cached=True` puts the Redis cache in front of it
    (a no-op when Redis is off or down); the health check asks for `cached=False`."""
    s = get_settings()
    if s.embedding_provider == "fake":
        inner: Embeddings = FakeEmbeddings(s.embedding_dim)
    elif s.embedding_provider == "tei":
        inner = TEIEmbeddings(s)
    elif s.embedding_provider == "jina":
        inner = JinaEmbeddings(s)
    else:
        inner = HFInferenceEmbeddings(s)
    if not cached:
        return inner
    from backend_app.embeddings.cached import CachedEmbeddings

    return CachedEmbeddings(inner, s.embedding_provider, s.embedding_model)


_health_cache: dict = {"ts": 0.0, "result": (False, "not checked")}


def check_embeddings(max_age_s: float = 300) -> tuple[bool, str]:
    """Tiny embed call; the result is cached for 5 minutes (in Redis, shared by all workers,
    and in memory as a fallback) so the health page doesn't burn HF quota. It bypasses the
    embedding cache, so it really tests the provider."""
    from backend_app.cache import store

    s_ = get_settings()
    health_key = f"embeddings:{s_.embedding_provider}:{s_.embedding_model}"
    shared = store.get_json("health", health_key)
    if shared is not None:
        return bool(shared[0]), str(shared[1])
    now = time.time()
    if now - _health_cache["ts"] < max_age_s:
        return _health_cache["result"]
    try:
        v = get_embedder(cached=False).embed_query("health check")
        result = (len(v) == get_settings().embedding_dim, f"ok ({len(v)}-dim)")
    except Exception as exc:  # noqa: BLE001
        result = (False, str(exc)[:200])
    _health_cache.update(ts=now, result=result)
    store.set_json("health", health_key, [result[0], result[1]])
    return result
