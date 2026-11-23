# 11 — The MCP server: the RAG as a tool for AI assistants

Files: [mcp-server/README.md](../../mcp-server/README.md) (client setup, the full reference),
[mcp_server/server.py](../../mcp-server/mcp_server/server.py),
[mcp_server/__main__.py](../../mcp-server/mcp_server/__main__.py),
[mcp_server/api_client.py](../../mcp-server/mcp_server/api_client.py),
[mcp-server/Containerfile](../../mcp-server/Containerfile).
(Compose service `mcp-server`, container `rag-mcp`, package `mcp_server`. It is part of the **frontend tier**:
a second "frontend" for AI clients next to the Dash UI.)

## What MCP is

The **Model Context Protocol** is a standard way for an AI app (Claude Code, Claude Desktop,
VS Code Copilot, your own agent) to discover and call **tools** and read **resources** that a
server offers. You write the server once, and any MCP client can use it. The client asks for
the tool list (name, description, JSON schema of the arguments). The model decides when to
call a tool, and the client sends the call to the server.

Our server is a thin adapter. Every tool is one HTTP call to the `backend`, so there is no second copy
of the RAG logic. The backend tags these calls `source:mcp` in Langfuse. Like the frontend, the MCP server has no
database settings: it ships its own log lines to the backend's `POST /logs/batch` (`service: mcp`), and `ask` /
`search` benefit from the backend's Redis cache.

## The five tools and the resource

| Tool | What it returns | API call |
|---|---|---|
| `ask(question, conversation_id?, mode="hybrid")` | `answer` with `[n]` markers, `citations`, `conversation_id`, `grounded`, `not_found`, `trace_url` | `POST /ask` |
| `search(query, mode="hybrid", top_k=5)` | Fused passages with dense/BM25 ranks. No LLM | `POST /search` |
| `list_documents()` | doc_id, source, title, chunk_count | `GET /documents` |
| `ingest_text(name, content)` | The ingestion job (saved as `uploads/<name>`) | `POST /documents/text?source=mcp` |
| `get_document(doc_id)` | Metadata + all chunks | `GET /documents/{doc_id}` |

Resource template: **`rag://documents/{doc_id}`** returns a document's chunks as markdown.

The tool descriptions in [server.py](../../mcp-server/mcp_server/server.py) are written for the
**model**. For example: "If `ask` returns not_found=true... say so rather than guessing". Tools also carry
annotations (`read_only_hint`, `idempotent_hint`) that tell the client which calls are safe.
Backend errors become `ToolError`s with a readable message.

## Transports: stdio vs streamable HTTP

| | stdio | Streamable HTTP |
|---|---|---|
| How | The client **starts the server as a child process** and talks over stdin/stdout | The server runs on its own; clients connect to `http://localhost:8100/mcp` |
| Who uses it | Local configs (Claude Desktop, Claude Code `--` command) | Remote clients, and the container |
| Where it runs | On your PC, from `mcp-server\.venv` | In the `rag-mcp` container (`MCP_TRANSPORT=http`) |
| Logs | To **stderr**, because stdout carries the protocol | To stdout (`podman logs rag-mcp`) |

The stdio-to-stderr rule matters: one stray `print()` to stdout corrupts the protocol stream.
`__main__.py` sets up logging before anything else for that reason. It sets it up again after
the SDK is constructed, because the SDK calls `logging.basicConfig` itself.

`/health` is a plain HTTP route (`custom_route`) used by the compose healthcheck.

## Optional `MCP_API_KEY`

Port 8100 is bound to `127.0.0.1`, so only your PC can reach it. If you ever expose it further,
set `MCP_API_KEY` in `.env` and run `.\tasks.ps1 restart mcp-server`. Then `APIKeyMiddleware` requires
`X-API-Key: <key>` or `Authorization: Bearer <key>` on every HTTP request except `/health`, and answers 401
otherwise. The comparison uses `hmac.compare_digest`, so response timing doesn't leak the key.
Separately, `API_KEY` protects the backend itself, and the MCP server forwards it as `X-API-Key`.

## Connecting clients

The exact configs are in [mcp-server/README.md](../../mcp-server/README.md). In short:

**Claude Code, HTTP** (server in the container):

```powershell
claude mcp add --transport http hybrid-rag http://localhost:8100/mcp
claude mcp list
```

**Claude Code, stdio** (Claude Code starts the server itself; needs `mcp-server\.venv`):

```powershell
claude mcp add hybrid-rag --env BACKEND_URL=http://localhost:8000 --env PYTHONPATH=C:\Users\mbdin\Desktop\hybrid-rag\mcp-server -- C:\Users\mbdin\Desktop\hybrid-rag\mcp-server\.venv\Scripts\python.exe -m mcp_server --transport stdio
```

`BACKEND_URL` is `http://localhost:8000` here because the server runs on Windows, not on `frontend-net`
(`API_URL` still works as the old name).

**Claude Desktop:** edit `%APPDATA%\Claude\claude_desktop_config.json` with an `mcpServers.hybrid-rag`
entry (command = the venv's `python.exe`, args `-m mcp_server --transport stdio`, env `BACKEND_URL` and
`PYTHONPATH`), then restart Claude Desktop.

**VS Code (Copilot agent mode):** `.vscode/mcp.json` with a server of `"type": "http"` and
`"url": "http://localhost:8100/mcp"`, or a stdio entry.

The README says the client configs were written from the clients' documented formats and not
tested against those apps. If one doesn't connect, start by checking the server with the commands below.

## The SDK 2.x rename: FastMCP → MCPServer

The official `mcp` Python SDK is pinned to **2.3.0** ([pyproject.toml](../../mcp-server/pyproject.toml)).
In 2.x the high-level server class `FastMCP` was renamed and moved:

```python
from mcp.server.mcpserver import MCPServer          # 1.x: from mcp.server.fastmcp import FastMCP
mcp = MCPServer("hybrid-rag", title=..., version="0.1.0", instructions=INSTRUCTIONS)
```

Tutorials written for 1.x use `FastMCP` and won't import with this version. The decorators
(`@mcp.tool`, `@mcp.resource`, `@mcp.custom_route`) work the same way.

## Try it

1. Is the HTTP server alive?
   ```powershell
   Invoke-RestMethod http://localhost:8100/health        # {status: ok, service: mcp}
   podman logs rag-mcp --tail 5
   ```
2. Connect Claude Code over HTTP (command above), then ask it:
   *"Use the hybrid-rag tools: what does error code E-271 mean? Show the sources."*
   It should call `ask` (or `search`). Then find the trace in Langfuse with the tag `source:mcp`.
3. Ask it to `list_documents`, then to read `rag://documents/<a doc_id>`.
4. Test the key: set `MCP_API_KEY=test123` in `.env`, run `.\tasks.ps1 restart mcp-server`, and check that
   `Invoke-WebRequest -Method Post http://localhost:8100/mcp` fails with **(401) Unauthorized**. Add the key to the client
   with `--header "X-API-Key: test123"`. Afterwards, clear the value and recreate the container again.
5. The integration test `test_mcp_tools_over_streamable_http` in
   [tests/integration/test_stack.py](../../tests/integration/test_stack.py) runs `list_documents` over HTTP:
   `.\tasks.ps1 itest` (it needs `mcp-server\.venv`).

## What to check

- [ ] `claude mcp list` shows `hybrid-rag` as connected.
- [ ] A tool call shows up in Langfuse with `source:mcp`.
- [ ] You can explain why stdio mode must never print to stdout.
- [ ] You know which import to use with mcp 2.x.
