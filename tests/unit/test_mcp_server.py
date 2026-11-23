"""Unit tests for the MCP server (mcp-server). The API is mocked with httpx.MockTransport and
tools are called through the mcp SDK's in-process `Client(server)`.

Run: mcp-server/.venv/Scripts/python -m pytest tests/unit/test_mcp_*.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp-server"))

anyio = pytest.importorskip("anyio")
httpx = pytest.importorskip("httpx")
pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from mcp_server.api_client import APIClient  # noqa: E402
from mcp_server.server import create_http_app, create_server  # noqa: E402

ASK_RESPONSE = {
    "answer": "Hybrid search fuses dense and BM25 results [1].",
    "citations": [
        {"n": 1, "chunk_id": "c1", "source": "rag.md", "heading_path": "RAG > Hybrid",
         "text": "long text", "cited": True},
        {"n": 2, "chunk_id": "c2", "source": "bm25.md", "heading_path": "BM25",
         "text": "other", "cited": False},
    ],
    "conversation_id": "conv-1", "rewritten_query": "q", "not_found": False, "grounded": True,
    "grounding_reason": "ok", "mode": "hybrid", "retrieved": [], "contexts": [],
    "latency_ms": 1234, "trace_id": "t-ask", "trace_url": "http://lf/trace/t-ask",
}
SEARCH_RESPONSE = {
    "dense": [], "bm25": [],
    "fused": [{"chunk_id": "c1", "score": 0.03, "source": "rag.md", "heading_path": "RAG",
               "position": 0, "doc_id": "d1", "text": "chunk text", "rank": 1,
               "dense_rank": 2, "bm25_rank": None}],
    "timings": {}, "trace_id": "t-s", "trace_url": None,
}
DOC = {"doc_id": "d1", "source": "rag.md", "name": "rag.md", "title": "RAG", "size": 10,
       "chunk_count": 1, "origin": "file", "status": "ok", "ingested_at": "2026-11-17",
       "chunks": [{"chunk_id": "c1", "position": 0, "heading_path": "RAG > Intro",
                   "text": "Hello chunk", "char_count": 11}]}


class FakeAPI:
    """Records requests and answers like the FastAPI service."""

    def __init__(self, fail: dict[str, httpx.Response] | None = None):
        self.requests: list[httpx.Request] = []
        self.fail = fail or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        if key in self.fail:
            return self.fail[key]
        routes = {
            "POST /ask": ASK_RESPONSE,
            "POST /search": SEARCH_RESPONSE,
            "GET /documents": [{k: v for k, v in DOC.items() if k != "chunks"}],
            "GET /documents/d1": DOC,
            "POST /documents/text": {"job_id": "j1", "status": "done",
                                     "results": [{"source": "uploads/n.md", "status": "ingested",
                                                  "chunks": 2, "chunks_new": 2}],
                                     "errors": [], "duration_s": 0.5, "trace_id": "t-j"},
        }
        if key in routes:
            return httpx.Response(200, json=routes[key])
        return httpx.Response(404, json={"detail": "document not found"})


def make(fake: FakeAPI, api_key: str | None = "secret"):
    api = APIClient("http://api.test", api_key=api_key, transport=httpx.MockTransport(fake))
    return create_server(api)


def run(coro_fn):
    return anyio.run(coro_fn)


def test_tool_list_and_schemas():
    server = make(FakeAPI())

    async def go():
        async with Client(server) as c:
            return (await c.list_tools()).tools, (await c.list_resource_templates())

    tools, templates = run(go)
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"ask", "search", "list_documents", "ingest_text", "get_document"}
    for t in tools:
        assert t.description and len(t.description) > 40
    ask = by_name["ask"].input_schema
    assert ask["required"] == ["question"]
    assert ask["properties"]["mode"]["enum"] == ["hybrid", "dense", "bm25"]
    assert ask["properties"]["mode"]["default"] == "hybrid"
    search = by_name["search"].input_schema
    assert search["properties"]["top_k"]["default"] == 5
    assert search["properties"]["top_k"]["maximum"] == 50
    assert by_name["ingest_text"].input_schema["required"] == ["name", "content"]
    assert by_name["get_document"].input_schema["required"] == ["doc_id"]
    assert by_name["search"].annotations.read_only_hint is True
    uris = [t.uri_template for t in templates.resource_templates]
    assert uris == ["rag://documents/{doc_id}"]


def test_ask_success_forwards_source_and_key():
    fake = FakeAPI()
    server = make(fake)

    async def go():
        async with Client(server) as c:
            return await c.call_tool("ask", {"question": "What is hybrid search?",
                                             "conversation_id": "conv-1", "mode": "bm25"})

    result = run(go)
    assert result.is_error is False
    out = result.structured_content
    assert out["answer"].startswith("Hybrid search")
    assert out["citations"] == [
        {"n": 1, "source": "rag.md", "heading_path": "RAG > Hybrid", "cited": True},
        {"n": 2, "source": "bm25.md", "heading_path": "BM25", "cited": False},
    ]
    assert out["conversation_id"] == "conv-1"
    assert out["grounded"] is True and out["not_found"] is False
    assert out["trace_url"] == "http://lf/trace/t-ask"

    req = fake.requests[-1]
    assert req.url.path == "/ask"
    assert req.headers["x-api-key"] == "secret"
    assert req.headers.get("x-request-id")
    body = json.loads(req.content)
    assert body["source"] == "mcp" and body["mode"] == "bm25"
    assert body["question"] == "What is hybrid search?" and body["conversation_id"] == "conv-1"


def test_search_list_ingest_get():
    fake = FakeAPI()
    server = make(fake, api_key=None)

    async def go():
        async with Client(server) as c:
            s = await c.call_tool("search", {"query": "bm25", "mode": "dense", "top_k": 3})
            ls = await c.call_tool("list_documents", {})
            ing = await c.call_tool("ingest_text", {"name": "n.md", "content": "# N\nhello"})
            doc = await c.call_tool("get_document", {"doc_id": "d1"})
            res = await c.read_resource("rag://documents/d1")
            return s, ls, ing, doc, res

    s, ls, ing, doc, res = run(go)
    row = s.structured_content["results"][0]
    assert row == {"rank": 1, "score": 0.03, "source": "rag.md", "heading_path": "RAG",
                   "text": "chunk text", "dense_rank": 2, "bm25_rank": None,
                   "doc_id": "d1", "chunk_id": "c1"}
    search_body = json.loads(fake.requests[0].content)
    assert search_body == {"query": "bm25", "mode": "dense", "top_k": 3, "source": "mcp"}
    assert "x-api-key" not in fake.requests[0].headers

    assert ls.structured_content == {"count": 1, "documents": [
        {"doc_id": "d1", "source": "rag.md", "title": "RAG", "chunk_count": 1}]}

    assert ing.structured_content["job_id"] == "j1"
    assert ing.structured_content["results"][0]["chunks"] == 2
    ingest_req = fake.requests[2]
    assert ingest_req.url.params["source"] == "mcp"
    assert json.loads(ingest_req.content) == {"name": "n.md", "content": "# N\nhello"}

    assert doc.structured_content["chunks"][0]["text"] == "Hello chunk"
    text = res.contents[0].text
    assert res.contents[0].mime_type == "text/markdown"
    assert "# RAG" in text and "RAG > Intro" in text and "Hello chunk" in text


def test_api_errors_become_tool_errors():
    fake = FakeAPI(fail={
        "POST /ask": httpx.Response(503, json={"detail": "GOOGLE_API_KEY is not set"}),
        "POST /search": httpx.Response(500, text="boom"),
    })
    server = make(fake)

    async def go():
        async with Client(server) as c:
            return (await c.call_tool("ask", {"question": "hi"}),
                    await c.call_tool("search", {"query": "x"}),
                    await c.call_tool("get_document", {"doc_id": "missing"}),
                    await c.call_tool("search", {"query": "x", "top_k": 999}))

    ask, search, missing, invalid = run(go)
    assert ask.is_error and "503" in ask.content[0].text and "GOOGLE_API_KEY" in ask.content[0].text
    assert search.is_error and "HTTP 500" in search.content[0].text
    assert missing.is_error and "list_documents" in missing.content[0].text
    assert invalid.is_error  # rejected by the input schema before reaching the API
    assert not any(r.url.path == "/search" and b"999" in r.content for r in fake.requests)
    for r in (ask, search, missing):
        assert "Traceback" not in r.content[0].text


def test_connection_error_is_readable():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    server = create_server(APIClient("http://api.test", transport=httpx.MockTransport(down)))

    async def go():
        async with Client(server) as c:
            return await c.call_tool("list_documents", {})

    result = run(go)
    assert result.is_error
    assert "Cannot reach the RAG API at http://api.test" in result.content[0].text


def test_missing_resource_raises_mcp_error():
    server = make(FakeAPI())

    from mcp.shared.exceptions import MCPError

    async def go():
        async with Client(server) as c:
            try:
                await c.read_resource("rag://documents/nope")
            except MCPError as exc:
                return exc
        return None

    err = run(go)
    assert err is not None and "not found" in str(err)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "pytest", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}


def test_http_auth_middleware():
    app = create_http_app(make(FakeAPI()), api_key="mcp-key")
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "service": "mcp"}

        r = client.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 401
        r = client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "X-API-Key": "wrong"})
        assert r.status_code == 401

        r = client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "X-API-Key": "mcp-key"})
        assert r.status_code == 200, r.text
        assert "hybrid-rag" in r.text

        r = client.post("/mcp", json=INIT,
                        headers={**MCP_HEADERS, "Authorization": "Bearer mcp-key"})
        assert r.status_code == 200, r.text


def test_http_without_key_is_open():
    app = create_http_app(make(FakeAPI()), api_key=None)
    with TestClient(app) as client:
        r = client.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 200, r.text
