# Hybrid RAG

Ask questions over your own `.md` / `.txt` files. Each question is answered with **dense search**
(Hugging Face embeddings in Qdrant) and **BM25 keyword search**. The two result lists are fused
with Reciprocal Rank Fusion, and **Google Gemini** writes the answer with citations. **LangGraph** runs
the flow, **MongoDB** stores documents, chats and logs, **Langfuse** traces every step, and **RAGAS**
measures quality, and **Redis** caches embeddings, searches and answers. You use it through a **Dash**
web UI (the **frontend**) or an **MCP server**; both only talk to the **backend**, which is the only part that
touches the data. Every service runs in its own **Podman** container.

The design and the reasons behind it are in [MASTER_PLAN.md](MASTER_PLAN.md). Notes for learning each
part are in [docs/learning/](docs/learning/README.md).

## Services

| URL | What |
|-----|------|
| http://localhost:8050 | **frontend**: Dash UI (Chat, Documents, Retrieval Debug, Evaluation, Logs, Settings/Health) |
| http://localhost:8000/docs | **backend**: API (FastAPI, interactive docs) |
| http://localhost:3000 | Langfuse: traces, sessions, scores, datasets (login in `.env.langfuse`) |
| http://localhost:6333/dashboard | Qdrant dashboard |
| localhost:6379 | Redis cache (`redis-cli`, password in `.env`) |
| http://localhost:8100/mcp | **mcp-server** (streamable HTTP) |
| http://localhost:8081 | mongo-express (dev only: `.\tasks.ps1 dev-tools`) |

All ports are bound to `127.0.0.1`, so only your PC can reach them.

## Quick start (Windows, PowerShell)

Prerequisites: Podman 6 with a running `podman machine`, WSL **2.7.5 or newer** (`wsl --version`; see
[troubleshooting](docs/learning/troubleshooting.md)), and Python 3.12 (`py -3.12`).

```powershell
.\tasks.ps1 setup      # .venv + podman-compose, creates .env and .env.langfuse (Langfuse secrets)
.\tasks.ps1 up         # builds images on first run, starts all containers
.\tasks.ps1 status     # every container should be "healthy"
```

On startup the backend ingests everything in `data/docs`: the sample knowledge base, 21 files and 56 chunks.

### Add your API keys

`.env` starts with the offline **fake** providers, so you can try everything without keys. To use the real models:

1. Edit `.env`:
   ```
   LLM_PROVIDER=gemini
   GOOGLE_API_KEY=<your Google AI Studio key>
   EMBEDDING_PROVIDER=hf_api
   HF_TOKEN=<your Hugging Face read token>
   ```
2. Recreate the backend (it reads `.env` when the container is created), then ingest again:
   ```powershell
   .\tasks.ps1 restart backend
   .\tasks.ps1 ingest
   ```
   Every chunk remembers which embedding provider and model made its vector, so switching from `fake` to
   `hf_api` re-embeds everything automatically (56 calls, in batches of 32). The Redis cache keeps real vectors
   for 30 days, so later re-ingests cost no Hugging Face calls. `.\tasks.ps1 ingest force` re-embeds every chunk
   even when nothing changed.
3. Check that **Settings / Health** in the UI shows `embeddings` and `llm` green.

`.env` and `.env.langfuse` are git-ignored, and secrets are passed in at runtime. They are never baked into an image.

## Production safeguards

The API uses Redis-backed per-client limits (120 requests/minute and 20 `/ask` requests/minute by default).
Adjust `RATE_LIMIT_*` in `.env`; it fails open if Redis is unavailable, so an optional cache failure cannot
take down the knowledge base. Set `API_KEY` and `MCP_API_KEY` before exposing the local ports beyond your PC.

Qdrant creates a retained local snapshot every 24 hours by default (`QDRANT_SNAPSHOT_*`). Inspect or create
one immediately through `GET /admin/backups/status` and `POST /admin/backups/qdrant`. MongoDB and Redis persist
in named volumes; use your normal host backup system for a copy of those volumes. Online evaluation remains a
deliberately manual operation through the `eval` job, so it cannot unexpectedly consume Gemini quota.

## Everyday commands

```powershell
.\tasks.ps1 logs backend          # follow one service's logs (backend, frontend, mcp-server, ...)
.\tasks.ps1 rebuild backend       # after changing code in backend/
.\tasks.ps1 restart backend       # after changing .env
.\tasks.ps1 ingest                # re-scan data/docs (unchanged files are skipped)
.\tasks.ps1 cache-stats           # Redis cache hits / misses / keys   (cache-clear [kind] empties it)
.\tasks.ps1 qdrant-backup         # snapshot -> data\qdrant_snapshots   (qdrant-restore <name> restores)
.\tasks.ps1 down                  # stop everything; data stays in the volumes
.\tasks.ps1 kube-up / kube-down   # run the app as two Podman pods (backend, frontend) from deploy/kube/
```

## Tests and evaluation

```powershell
.\tasks.ps1 test     # unit tests for backend, frontend, mcp-server, eval (no containers needed)
.\tasks.ps1 itest    # integration tests against the running stack (cache, log shipping, tier isolation, MCP)
.\tasks.ps1 eval --mode all --no-ragas          # cheap retrieval metrics, no LLM judge calls
.\tasks.ps1 eval --mode hybrid --limit 5        # RAGAS on 5 questions (uses Gemini)
```

Reports are written to `tests/eval/reports/`, the scores go to Langfuse, and the run summaries appear on the UI's
Evaluation page. A full RAGAS run makes several hundred judge calls, so on the Gemini free tier start with
`--limit` or `--metrics faithfulness,context_recall`. The eval job skips the Redis cache by default, so it always
measures the current pipeline (`--use-cache` turns the cache on for cheap re-runs).

## Connect an MCP client

```powershell
claude mcp add --transport http hybrid-rag http://localhost:8100/mcp     # Claude Code
```
Claude Desktop (stdio) and VS Code examples are in [mcp-server/README.md](mcp-server/README.md).

## Layout: two tiers

```
FRONTEND  (frontend-net)   frontend (Dash UI :8050)   mcp-server (MCP :8100)    -> talk to the backend over HTTP only
BACKEND   (+ backend-net)  backend (FastAPI + LangGraph :8000)                  -> the only container that touches data
DATA      (backend-net)    mongodb   qdrant   redis (cache)
OBSERV.   (langfuse-net)   langfuse-web/worker + postgres, clickhouse, redis, minio   (internal network)
JOBS                       eval (one-off RAGAS run, a client of the backend)
```

```
compose.yaml                 app: frontend, mcp-server, backend, mongodb, qdrant, redis, eval (profile), mongo-express (profile)
compose.observability.yaml   Langfuse v4: web, worker, postgres, clickhouse, redis, minio
backend/                     FastAPI + LangGraph (ingestion, BM25, dense, RRF, RAG graph, cache, tracing), package backend_app
frontend/                    Dash UI, package frontend_app
mcp-server/                  MCP server, package mcp_server
eval/                        RAGAS evaluation job, package eval_job
deploy/kube/                 the same app as two pods (backend, frontend) for `podman kube play`
data/docs/                   the knowledge base (sample: fictional company "Vellora Systems")
tests/                       unit/, integration/, eval/ (golden set + reports)
docs/learning/               one note per roadmap phase + troubleshooting
```
