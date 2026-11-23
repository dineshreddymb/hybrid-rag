"""Minimal LangChain `Embeddings` for RAGAS (answer relevancy). Mirrors
backend/backend_app/embeddings/providers.py so the judge uses the same model as the RAG.

- hf_api (default): Hugging Face Inference API, needs HF_TOKEN
- fake           : deterministic hash vectors, no network (tests)
"""
from __future__ import annotations

import hashlib
import logging
import math
import time

import httpx
from langchain_core.embeddings import Embeddings

from eval_job.config import EvalSettings

log = logging.getLogger(__name__)
RETRY_STATUS = (429, 500, 502, 503, 504)
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _pool(item) -> list[float]:
    """HF returns either one vector per text or one vector per token; mean-pool the latter."""
    if item and isinstance(item[0], list):
        dim = len(item[0])
        return [sum(tok[i] for tok in item) / len(item) for i in range(dim)]
    return item


class HFInferenceEmbeddings(Embeddings):
    def __init__(self, token: str, model: str, url_template: str, batch_size: int = 32,
                 timeout_s: float = 60.0, max_attempts: int = 5,
                 transport: httpx.BaseTransport | None = None):
        if not token:
            raise RuntimeError("HF_TOKEN is not set (or use EMBEDDING_PROVIDER=fake)")
        self.model = model
        self.url = url_template.format(model=model)
        self.batch_size = max(1, batch_size)
        self.max_attempts = max_attempts
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def _request(self, texts: list[str]) -> list:
        delay = 2.0
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self._http.post(self.url, headers=self._headers,
                                    json={"inputs": texts, "options": {"wait_for_model": True}})
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and "error" in data:
                    raise RuntimeError(f"HF API: {data['error']}")
                return data
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code in RETRY_STATUS and attempt < self.max_attempts:
                    try:
                        wait = float(exc.response.headers.get("retry-after", delay))
                    except ValueError:
                        wait = delay
                    log.warning("embedding retry: HTTP %s (attempt %s, wait %.0fs)",
                                code, attempt, wait)
                    time.sleep(min(wait, 30))
                    delay *= 2
                    continue
                raise RuntimeError(f"Embedding API error {code}: {exc.response.text[:300]}") from exc
            except httpx.TransportError as exc:
                if attempt < self.max_attempts:
                    log.warning("embedding retry: %s (attempt %s)", exc, attempt)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError("unreachable")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(_normalize(_pool(v)) for v in self._request(texts[i:i + self.batch_size]))
        return out

    def embed_query(self, text: str) -> list[float]:
        prefix = BGE_QUERY_PREFIX if "bge" in self.model.lower() else ""
        return self.embed_documents([prefix + text])[0]


class FakeEmbeddings(Embeddings):
    """Deterministic bag-of-words hash vectors (same algorithm as the API's fake provider)."""

    def __init__(self, dim: int = 384):
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


class JinaEmbeddings(Embeddings):
    def __init__(self, api_key: str, model: str, url: str, dim: int = 1024,
                 batch_size: int = 32, timeout_s: float = 60.0):
        if not api_key:
            raise RuntimeError("JINA_API_KEY is not set (or use EMBEDDING_PROVIDER=fake)")
        self.api_key = api_key
        self.model = model
        self.url = url
        self.dim = dim
        self.batch_size = max(1, batch_size)
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._http = httpx.Client(timeout=timeout_s)

    def _request_batch(self, texts: list[str], task: str) -> list[list[float]]:
        r = self._http.post(
            self.url,
            headers=self._headers,
            json={
                "model": self.model,
                "task": task,
                "dimensions": self.dim,
                "normalized": True,
                "embedding_type": "float",
                "input": texts,
            },
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            items = sorted(data["data"], key=lambda x: x.get("index", 0))
            return [it["embedding"] for it in items]
        raise RuntimeError(f"Unexpected Jina response: {data}")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            out.extend(self._request_batch(batch, "retrieval.passage"))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._request_batch([text], "retrieval.query")[0]


def get_embeddings(s: EvalSettings) -> Embeddings:
    if s.embedding_provider == "fake":
        return FakeEmbeddings(s.embedding_dim)
    if s.embedding_provider == "jina":
        return JinaEmbeddings(s.jina_api_key, s.embedding_model, s.jina_embedding_url,
                              dim=s.embedding_dim, batch_size=s.embedding_batch_size)
    return HFInferenceEmbeddings(s.hf_token, s.embedding_model, s.hf_api_url,
                                 batch_size=s.embedding_batch_size)
