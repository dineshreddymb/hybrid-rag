# Learning notes

One note per roadmap phase ([MASTER_PLAN.md §11](../../MASTER_PLAN.md)). Each note explains
**why**, then **how**, then gives a small **try it** exercise and a **what to check** list.
All commands are for **PowerShell** on Windows, run from the repo root
(`C:\Users\mbdin\Desktop\hybrid-rag`).

| Note | What you learn |
|------|----------------|
| [00-setup.md](00-setup.md) | Podman machine (WSL2 VM), images vs containers, `podman run/ps/logs/exec/rm`, the project venv |
| [01-data-services.md](01-data-services.md) | MongoDB + Qdrant: ports on 127.0.0.1, named volumes vs bind mounts, snapshots, networks, healthchecks |
| [02-observability-langfuse.md](02-observability-langfuse.md) | The 6-container Langfuse v4 stack, internal-only services, secrets in env files, `podman stats` |
| [03-api-container.md](03-api-container.md) | The backend Containerfile line by line, layer caching, non-root user, runtime secrets, `/health` |
| [04-ingestion.md](04-ingestion.md) | load → split → dedupe → embed → store, chunk ids, the `./data/docs` bind mount |
| [05-hybrid-retrieval.md](05-hybrid-retrieval.md) | Dense vs BM25 vs RRF fusion, with a worked example; the identifier-aware tokenizer |
| [06-langgraph-rag.md](06-langgraph-rag.md) | The query graph, state, checkpointer, prompts, grading and grounding, Langfuse spans, cost |
| [07-evaluation-ragas.md](07-evaluation-ragas.md) | Golden set, hit@k / MRR / abstention vs RAGAS, one-off job containers, Langfuse dataset runs |
| [08-dash-ui.md](08-dash-ui.md) | The six pages, frontend → backend only, gunicorn, `depends_on: service_healthy` |
| [09-compose-everything.md](09-compose-everything.md) | Two compose files, profiles, restart policies, rebuilding / recreating one service |
| [10-pods-kube.md](10-pods-kube.md) | `podman kube play`: pods, PVCs, hostPath, ConfigMap vs Secret, mapping to real Kubernetes |
| [11-mcp-server.md](11-mcp-server.md) | MCP, the five tools + resource, stdio vs HTTP, connecting Claude Code / Desktop / VS Code |
| [12-frontend-backend-and-cache.md](12-frontend-backend-and-cache.md) | Two tiers, three networks, logs via `/logs/batch`, the Redis cache (`index_version`, `use_cache`, fail-open) |
| [troubleshooting.md](troubleshooting.md) | Real problems hit while building this project: symptom → cause → fix |

## Mental model of the stack

```
 Your PC (Windows)                    Podman machine = a Linux VM running in WSL2
 ─────────────────                    ───────────────────────────────────────────────────────
                                      ┌───────────── frontend-net ─────────────┐
 Browser ── 127.0.0.1:8050 ─────────▶ │ [frontend Dash/gunicorn]               │
 Claude  ── 127.0.0.1:8100/mcp ─────▶ │ [mcp-server]        [eval] one-off job │
                                      └──────────────┬──── HTTP, by name "backend"
 PowerShell ─ 127.0.0.1:8000 ───────▶ [backend  FastAPI + LangGraph]  (on both app networks)
                                      ┌───────────── backend-net ──────────────┐
                                      │ [mongodb] docs, chunks, chat, logs     │ volume mongo_data
                                      │ [qdrant]  dense vectors                │ volume qdrant_storage
                                      │ [redis]   app cache                    │ volume redis_data
 Browser ── 127.0.0.1:3000 ─────────▶ │ [langfuse-web] ◀── traces / scores     │
                                      └────────────────────┬───────────────────┘
                                            langfuse-net (internal): [langfuse-worker] [postgres]
                                            [clickhouse] [langfuse-redis] [minio], each with a volume
 backend ──▶ Gemini API, HF Inference API (internet)
 One-off job: [eval] ──▶ backend /ask ──▶ report in tests/eval/reports
 Folders shared with the VM (bind mounts):
   data\docs              → /data/docs          (backend reads your .md/.txt files)
   data\qdrant_snapshots  → /qdrant/snapshots   (Qdrant backups you can see in Explorer)
   tests\eval             → /work/eval          (eval reads golden.jsonl, writes reports)
 Three networks: frontend-net, backend-net, langfuse-net. Only the backend touches the data stores
 (see 12-frontend-backend-and-cache.md).
```

Three ideas carry most of the weight:

1. **An image is a template, a container is a running copy.** You rebuild images when code
   changes. You recreate containers when config (`.env`) changes.
2. **Containers are disposable, volumes are not.** Delete every container and nothing is lost,
   because the data lives in named volumes and bind-mounted folders.
3. **On a shared network, services call each other by name** (`http://backend:8000`,
   `mongodb://mongodb:27017`), but only if they share a network. From Windows you use `localhost`
   and the published port.

## Where things differ from the plan

The plan was written before the code. Where they disagree, the notes follow the code:

- Langfuse runs **v4** (`langfuse/langfuse:4`), not v3. The Python SDK v5 needs a server ≥ 4.59.
- MongoDB is `mongo:8`, not `mongo:7`.
- MinIO's published port 9090 is the **S3 API** (for media URLs in the browser), not the MinIO console.
- Embeddings use a small `httpx` client in [providers.py](../../backend/backend_app/embeddings/providers.py),
  not `langchain-huggingface`. BM25 uses `rank_bm25` directly with its own tokenizer, not LangChain's `BM25Retriever`.
- The golden set uses `reference_sources` (file names), not `reference_contexts`.
- The MCP SDK 2.x class is `MCPServer` (the new name for `FastMCP`).
- The Chat page shows the answer when `/ask` returns. It does not stream.
