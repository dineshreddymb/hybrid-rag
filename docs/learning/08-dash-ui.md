# 08 — The Dash frontend

Files: [frontend/frontend_app/main.py](../../frontend/frontend_app/main.py),
[api_client.py](../../frontend/frontend_app/api_client.py), [pages/](../../frontend/frontend_app/pages/),
[logging_setup.py](../../frontend/frontend_app/logging_setup.py),
[frontend/Containerfile](../../frontend/Containerfile), the `frontend` service in [compose.yaml](../../compose.yaml)
(container `rag-frontend`, package `frontend_app`).

## Why a separate frontend container that only talks to the backend

All RAG logic lives in the `backend`. The frontend is a **client**, just like the MCP server and the eval job.
That gives you:

- **one source of truth**: a fix in the backend helps the frontend, MCP and eval at once,
- **small images**: the frontend image has Dash, Plotly and httpx, not LangChain (≈ 290 MB vs ≈ 430 MB),
- **independent restarts**: rebuilding the frontend doesn't touch the backend, and the other way round,
- **enforced tiers**: the frontend sits on `frontend-net` only, so it cannot even resolve `mongodb`
  (see [12](12-frontend-backend-and-cache.md)).

Every request goes through `ApiClient` ([api_client.py](../../frontend/frontend_app/api_client.py)) to
`BACKEND_URL=http://backend:8000` (the old name `API_URL` is still read as a fallback). It sends a fresh
`X-Request-ID`, plus `X-API-Key` if `API_KEY` is set. The backend logs the same request id, so a frontend log
row and a backend log row can be matched on the Logs page.
The frontend has **no database settings at all**. Its own log lines are batched and POSTed to the backend's
`/logs/batch`, which stores them in `app_logs` (best effort, from a background thread: a missing backend
never blocks a page).

## The six pages

| Page | URL | What it does | API calls |
|---|---|---|---|
| Chat | `/` | Ask questions, pick mode (hybrid/dense/BM25), see citations (grey = retrieved but not cited), a grounded badge, a **cached** badge when the answer came from Redis, an *Open trace* link and 👍/👎 | `/ask`, `/feedback`, `/conversations` |
| Documents | `/documents` | Upload `.md`/`.txt`, scan the docs folder (with a *Force* option), list docs and ingestion jobs, view chunks, re-ingest, delete | `/documents*`, `/ingest/scan`, `/ingestion-jobs` |
| Retrieval Debug | `/retrieval` | Dense vs BM25 vs fused tables side by side, sliders for top_k and dense weight ([05](05-hybrid-retrieval.md)); a **cached** badge for cached searches | `/search` |
| Evaluation | `/evaluation` | Eval runs from `eval_runs`, a metric comparison chart, per-item details, links to Langfuse | `/eval-runs` |
| Logs | `/logs` | Filter `app_logs` by service, level, request id and text, with an *open* link per row to its trace. Optional 5 s auto-refresh | `/logs` |
| Settings / Health | `/settings` | Green/red cards for MongoDB, Qdrant, Redis cache, HF embeddings, Gemini, Langfuse; the backend config; data counts; a cache panel (hits, misses, keys) with a **Clear cache** button. Refreshes every 30 s | `/health`, `/config`, `/stats`, `/admin/cache/stats`, `/admin/cache/clear` |

The navbar also links to Langfuse (`LANGFUSE_PUBLIC_URL`, default http://localhost:3000).
The Chat page shows the answer when `/ask` returns, with a loading indicator meanwhile. It doesn't stream
tokens (the plan said it would). The conversation id and messages are kept in the browser
(`dcc.Store(storage_type="session")`), so they survive a page reload but not a closed tab.
Old conversations live in Mongo.

Page registration: each module in `pages/` calls `dash.register_page(...)` when imported.
`main.py` imports them explicitly (`pages_folder=""`) instead of letting Dash auto-discover them,
because auto-discovery imports the modules a second time and registers every callback twice.

## gunicorn instead of `app.run()`

```dockerfile
CMD ["gunicorn", "-b", "0.0.0.0:8050", "--workers", "2", "--threads", "4", "--timeout", "180", "frontend_app.main:server"]
```

- `app.run()` starts Flask's development server. It is meant for one developer, not for serving.
- **gunicorn** is a production WSGI server. `frontend_app.main:server` is the Flask app inside Dash.
- `--workers 2 --threads 4`: up to 8 requests at once, so a slow `/ask` doesn't freeze the Logs page.
- `--timeout 180`: a worker that takes longer is killed. `/ask` with retries can take a while, so this is
  generous, and it matches the client's `ASK_TIMEOUT = 180`.
- Two worker **processes** don't share memory. That's fine here, because page state lives in the browser and data lives in the backend.

Locally, without a container: `python -m frontend_app.main` (from `frontend`) runs the dev server on port 8050.

## Start order: `depends_on` + healthy

```yaml
frontend:
  depends_on:
    backend:
      condition: service_healthy
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8050/healthz', timeout=4)"]
```

The frontend waits until the backend's healthcheck passes, and the backend in turn waits for Mongo, Qdrant and Redis.
So `up` starts things in the right order: data → backend → frontend/mcp-server. `/healthz` comes from Dash itself
(`health_endpoint="healthz"` in [main.py](../../frontend/frontend_app/main.py)). The check uses Python's
`urllib` because the slim image has no `curl`.

`depends_on` only matters **at start time**. If the backend dies later, the frontend keeps running and
shows "Cannot reach the API at http://backend:8000" until the backend is back.

## Try it

1. Open http://localhost:8050 and ask "What does error code E-217 mean?". Click a citation button to
   read the chunk, then click *Open trace*. Give a 👍 and find the `user_feedback` score on that trace in Langfuse.
2. Upload a small `.md` on Documents. It appears in `data\docs\uploads\` in Explorer and in the list.
   Delete it from the UI: it is removed from Mongo, Qdrant **and** disk (only for files in `uploads/`).
3. Logs page: filter `service = ui`, copy a request id, then filter by that request id. You see the
   matching `api` row too. (The `service` label in Mongo is still `ui` / `api` / `mcp`, even though the
   containers are now `frontend` / `backend` / `mcp-server`.)
4. Stop the backend and watch the frontend cope:
   ```powershell
   podman stop rag-backend
   # Settings page: "Cannot reach the API ..."; Chat shows an error instead of crashing
   podman start rag-backend
   ```
5. `podman logs rag-frontend --tail 20`: JSON log lines, one per API call with method, path, status and ms.

## What to check

- [ ] All six pages load. Settings shows MongoDB, Qdrant and Redis green.
- [ ] *Open trace* opens the right trace in Langfuse.
- [ ] `podman ps` shows `rag-frontend` as `(healthy)`, and it started after `rag-backend` became healthy.
- [ ] You can explain why the frontend image doesn't contain LangChain.
