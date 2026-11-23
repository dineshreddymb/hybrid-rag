# Hybrid RAG MCP server

Exposes the hybrid RAG to any MCP client. Every tool is a thin call to the FastAPI `backend`
service (no duplicated RAG logic); calls are tagged `source:mcp` in Langfuse.
Built on the official `mcp` Python SDK **2.3.0** (`mcp.server.mcpserver.MCPServer`, the v2 name
for FastMCP).

| Tool | What it does | API call |
|---|---|---|
| `ask(question, conversation_id?, mode="hybrid")` | RAG answer + citations, `conversation_id`, `grounded`, `not_found`, `trace_url` | `POST /ask` |
| `search(query, mode="hybrid", top_k=5)` | Fused passages with dense/BM25 ranks, no LLM | `POST /search` |
| `list_documents()` | doc_id, source, title, chunk_count | `GET /documents` |
| `ingest_text(name, content)` | Add/update a document, returns the ingestion job | `POST /documents/text?source=mcp` |
| `get_document(doc_id)` | Document metadata + chunks | `GET /documents/{doc_id}` |

Resource template: `rag://documents/{doc_id}` gives a document's chunks as markdown.

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `BACKEND_URL` (fallback `API_URL`) | `http://backend:8000` | Backend base URL (`http://localhost:8000` from your PC) |
| `API_KEY` | unset | Sent as `X-API-Key` to the API if set |
| `MCP_TRANSPORT` | `stdio` (container: `http`) | `stdio` or `http` |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8100` / `/mcp` | HTTP bind address and endpoint |
| `MCP_API_KEY` | unset | If set, HTTP requires `X-API-Key: <key>` or `Authorization: Bearer <key>` (`/health` stays open) |
| `LOG_LEVEL` | `INFO` | |

Logs are also shipped to the backend (`POST /logs/batch`, `service: "mcp"`), best effort. Logs are JSON lines: on **stderr** in stdio mode (stdout carries the protocol), on stdout in http mode.

## Run locally (Windows, from the repo root)

```powershell
py -3.12 -m venv mcp-server/.venv
mcp-server/.venv/Scripts/python -m pip install -e "mcp-server[dev]"   # or: pip install -r <deps from pyproject>

# streamable HTTP on http://localhost:8100/mcp (+ GET /health)
$env:BACKEND_URL="http://localhost:8000"; mcp-server/.venv/Scripts/python -m mcp_server --transport http --port 8100

# stdio (normally launched by the client, see below)
$env:BACKEND_URL="http://localhost:8000"; $env:PYTHONPATH="mcp-server"; mcp-server/.venv/Scripts/python -m mcp_server --transport stdio
```

Tests: `mcp-server/.venv/Scripts/python -m pytest tests/unit/test_mcp_*.py -q`

## Connect a client

Replace `C:\\path\\to\\hybrid-rag` with your checkout path.

### Claude Code

HTTP (server running in the container or locally):

```bash
claude mcp add --transport http hybrid-rag http://localhost:8100/mcp
# if MCP_API_KEY is set:
claude mcp add --transport http hybrid-rag http://localhost:8100/mcp --header "X-API-Key: <your-key>"
```

stdio (Claude Code launches the server itself):

```bash
claude mcp add hybrid-rag --env BACKEND_URL=http://localhost:8000 --env PYTHONPATH=C:\\path\\to\\hybrid-rag\\mcp-server -- C:\\path\\to\\hybrid-rag\\mcp-server\\.venv\\Scripts\\python.exe -m mcp_server --transport stdio
```

Check with `claude mcp list`, or `/mcp` inside Claude Code.

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Settings > Developer > Edit Config), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "hybrid-rag": {
      "command": "C:\\path\\to\\hybrid-rag\\mcp-server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server", "--transport", "stdio"],
      "env": {
        "BACKEND_URL": "http://localhost:8000",
        "PYTHONPATH": "C:\\path\\to\\hybrid-rag\\mcp-server"
      }
    }
  }
}
```

`PYTHONPATH` lets `-m mcp_server` work whatever the working directory is (Claude Desktop does
not start the process in the repo). You can drop it if you ran `pip install -e mcp-server`.
If the API needs a key, add `"API_KEY": "..."` to `env`.

### VS Code (GitHub Copilot agent mode)

`.vscode/mcp.json` in the workspace:

```json
{
  "inputs": [
    { "type": "promptString", "id": "mcp-key", "description": "Hybrid RAG MCP API key", "password": true }
  ],
  "servers": {
    "hybrid-rag": {
      "type": "http",
      "url": "http://localhost:8100/mcp",
      "headers": { "X-API-Key": "${input:mcp-key}" }
    },
    "hybrid-rag-stdio": {
      "type": "stdio",
      "command": "${workspaceFolder}/mcp-server/.venv/Scripts/python.exe",
      "args": ["-m", "mcp_server", "--transport", "stdio"],
      "env": { "BACKEND_URL": "http://localhost:8000", "PYTHONPATH": "${workspaceFolder}/mcp-server" }
    }
  }
}
```

Leave out `inputs`/`headers` when `MCP_API_KEY` is not set. (`.vscode/` is git-ignored in this repo.)

### Verification status

- Verified locally (2026-11-17, SDK 2.3.0): stdio subprocess and streamable HTTP via the SDK's own
  `Client` (initialize, tools/list, `list_documents`), the 401 without a key, Bearer/X-API-Key
  auth, and `/health`.
- **Not verified here:** the Claude Code, Claude Desktop and VS Code configs above were written from
  their documented formats and not run against those apps. The container image is built by `podman-compose` (`localhost/hybrid-rag-mcp`) and tested by `tests/integration/test_stack.py`.
