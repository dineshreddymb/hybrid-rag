"""Async HTTP client for the hybrid RAG FastAPI service.

All RAG logic lives in the API; this module only forwards calls and turns HTTP / network
failures into `APIError` with a message an LLM (and a human) can act on.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_API_URL = "http://backend:8000"
ASK_TIMEOUT_S = 180.0      # /ask runs retrieval + LLM generation + grounding check
DEFAULT_TIMEOUT_S = 60.0


class APIError(Exception):
    """A failed API call. `status` is the HTTP status, or None for network errors."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


def _detail(response: httpx.Response) -> str:
    """Extract FastAPI's {"detail": ...} (string or validation-error list), else raw text."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or response.reason_phrase or "").strip()[:500]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, list):  # pydantic validation errors
        parts = []
        for err in detail[:5]:
            if isinstance(err, dict):
                loc = ".".join(str(p) for p in err.get("loc", []) if p != "body")
                parts.append(f"{loc}: {err.get('msg', '')}".strip(": "))
            else:
                parts.append(str(err))
        return "; ".join(parts)
    return str(detail)[:500]


class APIClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, ask_timeout: float = ASK_TIMEOUT_S):
        self.base_url = (base_url or os.getenv("BACKEND_URL") or os.getenv("API_URL")
                         or DEFAULT_API_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else (os.getenv("API_KEY") or None)
        self._transport = transport  # injectable (httpx.MockTransport in tests)
        self.timeout = timeout
        self.ask_timeout = ask_timeout

    def _headers(self, request_id: str | None) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "hybrid-rag-mcp/0.1"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    async def request(self, method: str, path: str, *, json: Any = None,
                      params: dict[str, Any] | None = None, timeout: float | None = None,
                      request_id: str | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            # A short-lived client per call: cheap at MCP call rates, and it never outlives
            # the event loop (stdio, HTTP and tests each run their own loop).
            async with httpx.AsyncClient(transport=self._transport,
                                         timeout=timeout or self.timeout) as client:
                response = await client.request(method, url, json=json, params=params,
                                                headers=self._headers(request_id))
        except httpx.TimeoutException:
            raise APIError(f"The RAG API at {self.base_url} did not answer {method} {path} "
                           f"within {timeout or self.timeout:.0f} s. Try again later.") from None
        except httpx.HTTPError as exc:
            raise APIError(f"Cannot reach the RAG API at {self.base_url} ({type(exc).__name__}: "
                           f"{exc}). Is the backend service running and BACKEND_URL correct?") from None

        if response.status_code >= 400:
            detail = _detail(response)
            code = response.status_code
            if code == 401:
                msg = f"The RAG API rejected the API key (401): {detail}. Check API_KEY."
            elif code == 404:
                msg = f"Not found (404): {detail}."
            elif code == 422:
                msg = f"The RAG API rejected the arguments (422): {detail}."
            elif code == 503:
                msg = f"The RAG API is not ready (503): {detail}."
            else:
                msg = f"The RAG API returned HTTP {code} for {method} {path}: {detail}"
            raise APIError(msg, status=code)
        try:
            return response.json()
        except ValueError:
            raise APIError(f"The RAG API returned a non-JSON response for {method} {path}.",
                           status=response.status_code) from None

    # --- endpoints ------------------------------------------------------------------
    async def ask(self, question: str, conversation_id: str | None, mode: str,
                  request_id: str | None = None) -> dict:
        body = {"question": question, "conversation_id": conversation_id, "mode": mode,
                "source": "mcp", "use_history": True}
        return await self.request("POST", "/ask", json=body, timeout=self.ask_timeout,
                                  request_id=request_id)

    async def search(self, query: str, mode: str, top_k: int,
                     request_id: str | None = None) -> dict:
        body = {"query": query, "mode": mode, "top_k": top_k, "source": "mcp"}
        return await self.request("POST", "/search", json=body, request_id=request_id)

    async def list_documents(self, request_id: str | None = None) -> list[dict]:
        return await self.request("GET", "/documents", request_id=request_id)

    async def get_document(self, doc_id: str, request_id: str | None = None) -> dict:
        return await self.request("GET", f"/documents/{quote(doc_id, safe='')}",
                                  request_id=request_id)

    async def ingest_text(self, name: str, content: str, request_id: str | None = None) -> dict:
        return await self.request("POST", "/documents/text", params={"source": "mcp"},
                                  json={"name": name, "content": content},
                                  timeout=self.ask_timeout, request_id=request_id)
