"""Thin HTTP client for the FastAPI backend.

Every call is logged (method, path, status, ms) and carries an `X-Request-ID`, so a UI log
row and the API log row for the same request share one request_id.
Errors are raised as `ApiError` with a human-readable message the pages can show.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

import httpx

from .logging_setup import setup_logging

log = setup_logging()

DEFAULT_TIMEOUT = 30.0
ASK_TIMEOUT = 180.0


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, path: str = "",
                 detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.path = path
        self.detail = detail if detail is not None else message

    def __str__(self) -> str:
        return self.message


def _detail(resp: httpx.Response) -> str:
    """Turn FastAPI error bodies ({"detail": str | list}) into one line of text."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500] or resp.reason_phrase
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, list):  # pydantic validation errors
        parts = []
        for d in detail:
            if isinstance(d, dict):
                loc = ".".join(str(x) for x in d.get("loc", []) if x != "body")
                parts.append(f"{loc}: {d.get('msg', '')}" if loc else str(d.get("msg", d)))
            else:
                parts.append(str(d))
        return "; ".join(parts)
    return str(detail)


class ApiClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 transport: httpx.BaseTransport | None = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.base_url = (base_url or os.getenv("BACKEND_URL") or os.getenv("API_URL")
                         or "http://backend:8000").rstrip("/")
        key = os.getenv("API_KEY", "") if api_key is None else api_key
        headers = {"X-API-Key": key} if key else {}
        self._http = httpx.Client(base_url=self.base_url, headers=headers,
                                  timeout=timeout, transport=transport)

    # --- core ---------------------------------------------------------------------------
    def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                files: Any = None, timeout: float | None = None) -> Any:
        rid = uuid.uuid4().hex[:16]
        if params:
            params = {k: v for k, v in params.items() if v not in (None, "")}
        started = time.perf_counter()
        status: int | None = None
        try:
            kwargs: dict[str, Any] = {"json": json, "params": params, "files": files,
                                      "headers": {"X-Request-ID": rid}}
            if timeout is not None:
                kwargs["timeout"] = timeout
            resp = self._http.request(method, path, **kwargs)
            status = resp.status_code
        except httpx.TimeoutException as exc:
            raise ApiError(f"The API did not answer in time ({method} {path}).",
                           None, path) from exc
        except httpx.HTTPError as exc:
            raise ApiError(f"Cannot reach the API at {self.base_url} ({exc.__class__.__name__}).",
                           None, path) from exc
        finally:
            ms = round((time.perf_counter() - started) * 1000, 1)
            level = 20 if status is not None and status < 400 else 30
            log.log(level, "api_call", extra={"method": method, "path": path, "status": status,
                                              "ms": ms, "request_id": rid})
        if status >= 400:
            detail = _detail(resp)
            raise ApiError(f"API error {status}: {detail}", status, path, detail)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params or None)

    # --- health & config ----------------------------------------------------------------
    def health(self) -> dict:
        return self.get("/health")

    def config(self) -> dict:
        return self.get("/config")

    def stats(self) -> dict:
        return self.get("/stats")

    def cache_stats(self) -> dict:
        return self.get("/admin/cache/stats")

    def clear_cache(self, kind: str = "all") -> dict:
        return self.request("POST", "/admin/cache/clear", params={"kind": kind})

    # --- RAG ------------------------------------------------------------------------------
    def ask(self, question: str, conversation_id: str | None = None, mode: str = "hybrid",
            top_k: int | None = None, weight_dense: float | None = None,
            fast_mode: bool | None = None) -> dict:
        body = {"question": question, "conversation_id": conversation_id, "mode": mode,
                "source": "ui", "use_history": True}
        if top_k is not None:
            body["top_k"] = top_k
        if weight_dense is not None:
            body["weight_dense"] = weight_dense
        if fast_mode is not None:
            body["fast_mode"] = fast_mode
        return self.request("POST", "/ask", json=body, timeout=ASK_TIMEOUT)

    def search(self, query: str, mode: str = "hybrid", top_k: int | None = None,
               weight_dense: float | None = None, top_k_dense: int | None = None,
               top_k_bm25: int | None = None) -> dict:
        body = {"query": query, "mode": mode, "top_k": top_k, "weight_dense": weight_dense,
                "top_k_dense": top_k_dense, "top_k_bm25": top_k_bm25, "source": "ui"}
        return self.request("POST", "/search",
                            json={k: v for k, v in body.items() if v is not None},
                            timeout=ASK_TIMEOUT)

    def feedback(self, trace_id: str, value: int, comment: str | None = None) -> dict:
        body: dict[str, Any] = {"trace_id": trace_id, "value": 1 if value else 0}
        if comment:
            body["comment"] = comment
        return self.request("POST", "/feedback", json=body)

    def conversations(self, limit: int = 50) -> list[dict]:
        return self.get("/conversations", limit=limit) or []

    def messages(self, conversation_id: str) -> list[dict]:
        return self.get(f"/conversations/{conversation_id}/messages") or []

    # --- documents --------------------------------------------------------------------------
    def documents(self) -> list[dict]:
        return self.get("/documents") or []

    def document(self, doc_id: str) -> dict:
        return self.get(f"/documents/{doc_id}")

    def delete_document(self, doc_id: str) -> dict:
        return self.request("DELETE", f"/documents/{doc_id}")

    def reingest(self, doc_id: str) -> dict:
        return self.request("POST", f"/documents/{doc_id}/reingest", timeout=ASK_TIMEOUT)

    def upload(self, files: list[tuple[str, bytes]]) -> dict:
        """files = [(filename, raw bytes), ...] → multipart field `files`."""
        parts = [("files", (name, data, "text/markdown" if name.lower().endswith(".md")
                            else "text/plain")) for name, data in files]
        return self.request("POST", "/documents/upload", files=parts, timeout=ASK_TIMEOUT)

    def scan(self, force: bool = False) -> dict:
        return self.request("POST", "/ingest/scan", params={"force": "true"} if force else None,
                            timeout=ASK_TIMEOUT)

    def ingestion_jobs(self, limit: int = 50) -> list[dict]:
        return self.get("/ingestion-jobs", limit=limit) or []

    def sync_remote(self, force: bool = False, url: str | None = None) -> dict:
        params = {}
        if force:
            params["force"] = "true"
        if url:
            params["url"] = url
        return self.request("POST", "/documents/sync-remote", params=params or None,
                            timeout=ASK_TIMEOUT)

    def sync_status(self) -> dict:
        return self.get("/documents/sync-status") or {}

    def resolve_conflict(self, source: str, action: str, new_name: str | None = None) -> dict:
        return self.request(
            "POST",
            "/documents/conflict/action",
            json={"source": source, "action": action, "new_name": new_name},
            timeout=ASK_TIMEOUT,
        )


    # --- logs & eval --------------------------------------------------------------------------
    def logs(self, service: str | None = None, level: str | None = None,
             request_id: str | None = None, q: str | None = None, limit: int = 200) -> list[dict]:
        return self.get("/logs", service=service, level=level, request_id=request_id, q=q,
                        limit=limit) or []

    def eval_runs(self, limit: int = 100) -> list[dict]:
        return self.get("/eval-runs", limit=limit) or []

    def eval_run(self, run_id: str) -> dict:
        return self.get(f"/eval-runs/{run_id}")


_client: ApiClient | None = None


def get_client() -> ApiClient:
    """One shared client per process (httpx.Client is thread-safe)."""
    global _client
    if _client is None:
        _client = ApiClient()
    return _client
