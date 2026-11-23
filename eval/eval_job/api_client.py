"""Thin HTTP client for the RAG API (health, config, ask, eval-runs)."""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class ApiUnavailable(RuntimeError):
    """The API is unreachable or reports itself unhealthy."""


class ApiClient:
    def __init__(self, base_url: str, api_key: str = "", ask_timeout_s: float = 180.0,
                 transport: httpx.BaseTransport | None = None):
        headers = {"X-API-Key": api_key} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers,
                                  timeout=httpx.Timeout(30.0), transport=transport)
        self.ask_timeout_s = ask_timeout_s

    def close(self) -> None:
        self._http.close()

    def health(self) -> dict:
        """GET /health; raises ApiUnavailable when unreachable or core stores are down."""
        try:
            r = self._http.get("/health", timeout=15.0)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ApiUnavailable(f"API not reachable at {self._http.base_url}: {exc}") from exc
        if data.get("status") != "ok":
            raise ApiUnavailable(f"API unhealthy: {data}")
        return data

    def config(self) -> dict:
        try:
            r = self._http.get("/config")
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("GET /config failed: %s", exc)
            return {}

    def ask(self, question: str, mode: str, top_k: int | None = None,
            retries: int = 2, use_cache: bool = False) -> dict:
        """POST /ask with a fresh conversation. Retries on 429/5xx/timeouts with backoff."""
        body = {"question": question, "mode": mode, "source": "eval", "use_history": False,
                "use_cache": use_cache}
        if top_k:
            body["top_k"] = top_k
        delay = 5.0
        for attempt in range(retries + 1):
            try:
                r = self._http.post("/ask", json=body, timeout=self.ask_timeout_s)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    log.warning("/ask HTTP %s, retrying in %.0fs", r.status_code, delay)
                    time.sleep(delay)
                    delay *= 3
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < retries:
                    log.warning("/ask %s, retrying in %.0fs", exc.__class__.__name__, delay)
                    time.sleep(delay)
                    delay *= 3
                    continue
                raise
        raise RuntimeError("unreachable")

    def post_eval_run(self, doc: dict) -> str | None:
        try:
            r = self._http.post("/eval-runs", json=doc)
            r.raise_for_status()
            return r.json().get("run_id")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("POST /eval-runs failed: %s", exc)
            return None
