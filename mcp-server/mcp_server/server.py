"""MCP server (mcp SDK 2.x `MCPServer`, formerly FastMCP) exposing the hybrid RAG.

Tools: ask, search, list_documents, ingest_text, get_document.
Resource template: rag://documents/{doc_id}  (a document's chunks as markdown).

Every tool forwards to the FastAPI service (no duplicated RAG logic). The API tags these
calls `source:mcp` in Langfuse.
"""
from __future__ import annotations

import hmac
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_server.api_client import APIClient, APIError
from mcp_server.logging_setup import get_logger, request_id_var

log = get_logger("mcp_server.tools")

Mode = Literal["hybrid", "dense", "bm25"]

INSTRUCTIONS = """\
This server answers questions from a private knowledge base (markdown/text documents) using
hybrid retrieval (dense vectors + BM25 keyword search, fused) and an LLM that cites its sources.

- Use `ask` for a natural-language answer with numbered citations. Pass the returned
  `conversation_id` back on follow-up questions so the server can resolve references.
- Use `search` to see the raw retrieved passages (no LLM), e.g. to quote or verify text.
- Use `list_documents` to see what the knowledge base contains, `get_document` (or the
  resource rag://documents/{doc_id}) to read one document's chunks, and `ingest_text` to add
  a new document.
If `ask` returns not_found=true, the knowledge base does not contain the answer: say so
rather than guessing."""


async def _run_tool(tool: str, fn: Callable[[str], Awaitable[dict]]) -> dict:
    """Run one tool call: request id, timing, logging, API errors -> ToolError."""
    rid = uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    started = time.perf_counter()
    try:
        result = await fn(rid)
        log.info("tool_call", extra={"tool": tool, "ok": True, "trace_id": result.get("trace_id"),
                                     "ms": round((time.perf_counter() - started) * 1000, 1)})
        return result
    except APIError as exc:
        log.warning("tool_call", extra={"tool": tool, "ok": False, "error": exc.message,
                                        "status": exc.status,
                                        "ms": round((time.perf_counter() - started) * 1000, 1)})
        raise ToolError(exc.message) from None
    except ToolError:
        raise
    except Exception as exc:  # unexpected: log it, give the client a readable message
        log.exception("tool_call", extra={"tool": tool, "ok": False, "error": repr(exc),
                                          "ms": round((time.perf_counter() - started) * 1000, 1)})
        raise ToolError(f"{tool} failed unexpectedly: {type(exc).__name__}: {exc}") from None
    finally:
        request_id_var.reset(token)


def _chunks_markdown(doc: dict) -> str:
    title = doc.get("title") or doc.get("name") or doc.get("source") or doc.get("doc_id")
    lines = [f"# {title}", "",
             f"- doc_id: `{doc.get('doc_id')}`",
             f"- source: `{doc.get('source')}`",
             f"- chunks: {len(doc.get('chunks', []))}", ""]
    for c in doc.get("chunks", []):
        heading = c.get("heading_path") or "(no heading)"
        lines += [f"## Chunk {c.get('position')} — {heading}", "", (c.get("text") or "").strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def create_server(api: APIClient | None = None) -> MCPServer:
    """Build the MCP server. `api` is injectable so tests can use httpx.MockTransport."""
    api = api or APIClient()
    mcp = MCPServer("hybrid-rag", title="Hybrid RAG knowledge base", version="0.1.0",
                    instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @mcp.tool(
        title="Ask the knowledge base",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        description=(
            "Answer a question from the knowledge base with retrieval-augmented generation. "
            "Returns `answer` (markdown, with [n] markers) and `citations` "
            "(n, source file, heading_path, cited=true if the answer actually uses it), plus "
            "`conversation_id` (pass it back for follow-up questions), `grounded` (whether a "
            "checker found the answer supported by the sources), `not_found` (true when the "
            "knowledge base has no answer - then say so instead of guessing) and `trace_url` "
            "(Langfuse trace). Can take up to about a minute."
        ),
    )
    async def ask(
        question: Annotated[str, Field(description="The question, in natural language.",
                                       min_length=1, max_length=2000)],
        conversation_id: Annotated[str | None, Field(
            description="conversation_id from a previous `ask` result to continue that "
                        "conversation; omit to start a new one.")] = None,
        mode: Annotated[Mode, Field(
            description="Retrieval mode: 'hybrid' (dense + BM25, best default), 'dense' "
                        "(semantic only) or 'bm25' (keyword only).")] = "hybrid",
    ) -> dict[str, Any]:
        async def call(rid: str) -> dict:
            r = await api.ask(question, conversation_id, mode, request_id=rid)
            return {
                "answer": r.get("answer", ""),
                "citations": [{k: c.get(k) for k in ("n", "source", "heading_path", "cited")}
                              for c in r.get("citations") or []],
                "conversation_id": r.get("conversation_id"),
                "grounded": r.get("grounded"),
                "not_found": r.get("not_found"),
                "trace_url": r.get("trace_url"),
                "trace_id": r.get("trace_id"),
            }

        return await _run_tool("ask", call)

    @mcp.tool(
        title="Search passages",
        annotations=read_only,
        description=(
            "Retrieve the most relevant passages for a query WITHOUT generating an answer. "
            "Returns `results`: rank, fused score, source file, heading_path, the passage "
            "text, and its rank in the dense (semantic) and BM25 (keyword) lists (null if it "
            "was not retrieved by that retriever), plus doc_id/chunk_id. Use it to quote "
            "or verify exact text, or to see why `ask` answered as it did."
        ),
    )
    async def search(
        query: Annotated[str, Field(description="Search query (keywords or a question).",
                                    min_length=1, max_length=2000)],
        mode: Annotated[Mode, Field(
            description="'hybrid' (dense + BM25 fused), 'dense' or 'bm25'.")] = "hybrid",
        top_k: Annotated[int, Field(description="Number of passages to return (1-50).",
                                    ge=1, le=50)] = 5,
    ) -> dict[str, Any]:
        async def call(rid: str) -> dict:
            r = await api.search(query, mode, top_k, request_id=rid)
            rows = r.get("fused") or []
            results = [{
                "rank": row.get("rank", i),
                "score": row.get("score"),
                "source": row.get("source"),
                "heading_path": row.get("heading_path"),
                "text": row.get("text"),
                "dense_rank": row.get("dense_rank"),
                "bm25_rank": row.get("bm25_rank"),
                "doc_id": row.get("doc_id"),
                "chunk_id": row.get("chunk_id"),
            } for i, row in enumerate(rows, start=1)]
            return {"query": query, "mode": mode, "results": results,
                    "trace_url": r.get("trace_url"), "trace_id": r.get("trace_id")}

        return await _run_tool("search", call)

    @mcp.tool(
        title="List documents",
        annotations=read_only,
        description=(
            "List every document in the knowledge base: doc_id, source (file path), title "
            "and chunk_count. Use a doc_id with `get_document` or the resource "
            "rag://documents/{doc_id}."
        ),
    )
    async def list_documents() -> dict[str, Any]:
        async def call(rid: str) -> dict:
            docs = await api.list_documents(request_id=rid)
            items = [{"doc_id": d.get("doc_id"), "source": d.get("source"),
                      "title": d.get("title") or d.get("name"),
                      "chunk_count": d.get("chunk_count")} for d in docs or []]
            return {"count": len(items), "documents": items}

        return await _run_tool("list_documents", call)

    @mcp.tool(
        title="Add a text document",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                    idempotent_hint=True, open_world_hint=False),
        description=(
            "Add (or update) a document in the knowledge base from raw markdown or plain text. "
            "It is saved as uploads/<name> (a .md suffix is added if missing), chunked, "
            "embedded and indexed for both dense and BM25 search; re-sending the same name "
            "replaces that document, unchanged content is skipped. Returns the ingestion job "
            "summary (status, chunks created, errors)."
        ),
    )
    async def ingest_text(
        name: Annotated[str, Field(description="File name, e.g. 'meeting-notes.md'.",
                                   min_length=1, max_length=200)],
        content: Annotated[str, Field(description="The full document text (markdown "
                                      "headings are used for chunking and citations).",
                                      min_length=1, max_length=500_000)],
    ) -> dict[str, Any]:
        async def call(rid: str) -> dict:
            job = await api.ingest_text(name, content, request_id=rid)
            return {k: job.get(k) for k in ("job_id", "status", "results", "errors",
                                             "duration_s", "trace_id")}

        return await _run_tool("ingest_text", call)

    @mcp.tool(
        title="Read a document",
        annotations=read_only,
        description=(
            "Get one document's metadata and all of its chunks in order (chunk_id, position, "
            "heading_path, text, char_count). Get doc_id values from `list_documents` or from "
            "`search` results."
        ),
    )
    async def get_document(
        doc_id: Annotated[str, Field(description="The document's doc_id.", min_length=1,
                                     max_length=200)],
    ) -> dict[str, Any]:
        async def call(rid: str) -> dict:
            try:
                return await api.get_document(doc_id, request_id=rid)
            except APIError as exc:
                if exc.status == 404:
                    raise APIError(f"Document '{doc_id}' not found. Use list_documents to see "
                                   "valid doc_ids.", status=404) from None
                raise

        return await _run_tool("get_document", call)

    @mcp.resource(
        "rag://documents/{doc_id}",
        name="document",
        title="Knowledge-base document",
        description="All chunks of one knowledge-base document, as markdown.",
        mime_type="text/markdown",
    )
    async def document_resource(doc_id: str) -> str:
        rid = uuid.uuid4().hex[:16]
        started = time.perf_counter()
        try:
            doc = await api.get_document(doc_id, request_id=rid)
        except APIError as exc:
            log.warning("resource_read", extra={"uri": f"rag://documents/{doc_id}", "ok": False,
                                                "error": exc.message, "request_id": rid})
            if exc.status == 404:
                raise ResourceNotFoundError(f"Document '{doc_id}' not found.") from None
            raise ResourceError(exc.message) from None
        log.info("resource_read", extra={"uri": f"rag://documents/{doc_id}", "ok": True,
                                         "request_id": rid,
                                         "ms": round((time.perf_counter() - started) * 1000, 1)})
        return _chunks_markdown(doc)

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok", "service": "mcp"})

    return mcp


class APIKeyMiddleware:
    """ASGI middleware: require `X-API-Key: <key>` or `Authorization: Bearer <key>`.

    /health stays open (container healthchecks). Lifespan events pass straight through.
    """

    def __init__(self, app: ASGIApp, api_key: str, open_paths: tuple[str, ...] = ("/health",)):
        self.app = app
        self._key = api_key.encode()
        self.open_paths = open_paths

    def _authorized(self, scope: Scope) -> bool:
        headers = {k.decode("latin-1").lower(): v for k, v in scope.get("headers", [])}
        supplied = headers.get("x-api-key")
        if supplied is None:
            auth = headers.get("authorization", b"")
            if auth[:7].lower() == b"bearer ":
                supplied = auth[7:].strip()
        return supplied is not None and hmac.compare_digest(supplied, self._key)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") not in self.open_paths \
                and scope.get("method") != "OPTIONS" and not self._authorized(scope):
            response = JSONResponse(
                {"error": "unauthorized",
                 "detail": "missing or invalid API key (send X-API-Key or Authorization: Bearer)"},
                status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_http_app(server: MCPServer | None = None, *, path: str = "/mcp",
                    host: str = "0.0.0.0", api_key: str | None = None) -> ASGIApp:
    """Streamable-HTTP ASGI app (+ /health), wrapped in APIKeyMiddleware if a key is set."""
    server = server or create_server()
    app: ASGIApp = server.streamable_http_app(streamable_http_path=path, host=host)
    if api_key:
        app = APIKeyMiddleware(app, api_key)
    return app
