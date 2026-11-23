# 12 — Frontend / backend tiers, three networks, and the Redis cache

Files: [compose.yaml](../../compose.yaml), [compose.observability.yaml](../../compose.observability.yaml),
[cache/store.py](../../backend/backend_app/cache/store.py), [cache/redis_client.py](../../backend/backend_app/cache/redis_client.py),
[embeddings/cached.py](../../backend/backend_app/embeddings/cached.py), [graph/service.py](../../backend/backend_app/graph/service.py),
[retrieval/service.py](../../backend/backend_app/retrieval/service.py), [MASTER_PLAN.md §15](../../MASTER_PLAN.md).

## Why two tiers

| Tier | Containers | Folder | Job |
|---|---|---|---|
| Frontend | `frontend` (Dash, :8050), `mcp-server` (:8100) | `frontend/`, `mcp-server/` | Thin clients for people and for AI assistants. They only speak HTTP to the backend |
| Backend | `backend` (FastAPI + LangGraph, :8000) | `backend/` | Ingestion, retrieval, RAG, cache, tracing. **The only container that touches data** |
| Data | `mongodb`, `qdrant`, `redis` | (images) | Storage and cache, reachable only from the backend tier |
| Job | `eval` | `eval/` | One-off RAGAS run, a client of the backend |

Before v4 everything sat on one network, so the UI could have talked to MongoDB directly (and it even wrote its logs
there). Now the rule "only the backend touches data" is enforced by Podman, not just by convention. A bug or a leaked
key in the web tier can't read your documents or your Redis, and the frontend holds no database settings or model keys.

## Three networks

```
 Browser ─:8050─▶ ┌──────────── frontend-net ────────────┐
 MCP client ─:8100▶│  [frontend]   [mcp-server]   [eval]  │
                  │        └───── HTTP ─────┐            │
                  └─────────────────────────┼────────────┘
                                            ▼
                  ┌──────────── backend-net ─────────────────────────────┐
                  │               [backend :8000] ──▶ Gemini, HF API      │
                  │           ┌──────┼──────┬──────────┐                  │
                  │      [mongodb] [qdrant] [redis] [langfuse-web :3000]  │
                  └───────────────────────────────────────┼──────────────┘
                                                          │ langfuse-net (internal):
                                                          │ worker, postgres, clickhouse,
                                                          │ redis, minio
```

- `frontend-net`: frontend, mcp-server, eval, backend.
- `backend-net`: backend, mongodb, qdrant, redis, mongo-express (dev), `langfuse-web`, and eval (it writes scores to Langfuse).
- `langfuse-net` (`internal: true`): every Langfuse container. Its own Redis, ClickHouse and Postgres are not reachable by anything else.
- `backend` is the only app container on both app networks. Podman's DNS only answers for names on a network the asking container has joined.

Each container also gets only the variables it needs: the backend reads the whole `.env`; the frontend gets `BACKEND_URL`,
`API_KEY`, `LANGFUSE_PUBLIC_URL`, `LOG_LEVEL`; `mcp-server` adds `MCP_API_KEY` and `MCP_TRANSPORT`.

### Try it: the frontend cannot see the data

```powershell
podman exec rag-frontend python -c "import socket; print(socket.gethostbyname('backend'))"   # works: an IP
podman exec rag-frontend python -c "import socket; print(socket.gethostbyname('mongodb'))"   # fails: gaierror, Name or service not known
podman exec rag-backend  python -c "import socket; print(socket.gethostbyname('mongodb'))"   # works: the backend is on backend-net
podman network inspect frontend-net backend-net --format "{{.Name}}: {{range .Containers}}{{.Name}} {{end}}"
```

The same test works for `qdrant` and `redis`. Also try `mcp-server` (`podman exec rag-mcp python -c ...`) and see the same result.

## How logs reach MongoDB without a database connection

The frontend and the MCP server write JSON lines to stdout as before (`podman logs rag-frontend`). In addition, a
background thread collects the log rows in a bounded queue and POSTs them in batches to **`POST /logs/batch`** on the
backend (`{"service": "ui" | "mcp" | "eval", "entries": [...]}`, up to 500 entries). The backend stores them in `app_logs`
next to its own rows, so the Logs page still shows every service. Shipping is best effort: if the backend is down, the rows
are dropped and no page ever waits for it. The `service` labels in Mongo are still `api`, `ui` and `mcp`, even though the
containers are called `backend`, `frontend` and `mcp-server`.

```powershell
podman exec rag-mongodb mongosh hybrid_rag --quiet --eval "db.app_logs.aggregate([{ `$group: { _id: '`$service', n: { `$sum: 1 } } }])"
```

## The Redis cache

A separate `redis` container (`redis:7-alpine`, volume `redis_data`) sits on `backend-net`. It is **not** Langfuse's Redis:
Langfuse's is a job queue and must never lose a key (`noeviction`), a cache should throw away the oldest keys when full.
Ours runs `--maxmemory 256mb --maxmemory-policy allkeys-lru --save 300 10` (an RDB snapshot to disk, so the embedding
cache survives restarts), with a password from `.env` (`REDIS_PASSWORD`, generated by `.\tasks.ps1 setup`).

| Cache | Key | Value | TTL | Saves |
|---|---|---|---|---|
| Embeddings | `emb:{provider}:{model}:{sha256}` (documents and queries use different hashes) | float32 vector | 30 days | Hugging Face calls when re-ingesting or repeating a query |
| Search | `search:{index_version}:{sha256(query, mode, top_k, weights, ...)}` | the ranked lists | 10 min | Retrieval work and one embedding call |
| Answers | `answer:{index_version}:{sha256(question, mode, top_k, weight, llm, prompt version, ...)}` | the whole `/ask` result | 24 h | 3-4 Gemini calls |
| Health | `health:{check}` | ok + detail | 5 min | Repeated embedding/LLM probes |

TTLs come from `CACHE_TTL_ANSWER_S`, `CACHE_TTL_SEARCH_S`, `CACHE_TTL_EMBEDDING_S` in `.env`. The provider and model are in
the embedding key, so offline `fake` vectors can never be served as real `hf_api` ones; the answer key contains the LLM
provider/model and the prompt version for the same reason.

**`index_version` stops stale answers.** A counter in Redis (`cache:index_version`) is increased whenever ingestion adds,
changes or removes chunks, or a document is deleted. Search and answer keys contain it, so after an upload the old
entries are simply never matched again and expire on their own. An ingestion where every file is `unchanged` does not bump it.

**The answer cache skips follow-ups.** `ask()` only looks in the cache when the conversation has **no history**. "And the
second one?" means something different in every chat, so caching it by text would return a wrong answer.

**`use_cache`.** `POST /ask` and `POST /search` accept `"use_cache": false` to bypass all caches for one request. The eval job
does that by default (`--use-cache` turns it on) so an evaluation always measures the current pipeline.

**Fail-open.** Redis calls have a 50 ms timeout. After a failure the backend skips Redis for 10 s, logs `redis_unavailable`,
and works without the cache. `/health` shows `redis` red but `status` stays `ok` (only Mongo and Qdrant decide that).
Langfuse tracing follows the same rule.

**Visibility.** `GET /admin/cache/stats` and `POST /admin/cache/clear?kind=answer|search|embedding|health|all`
(`.\tasks.ps1 cache-stats`, `cache-clear [kind]`). The Chat page marks cached answers with a **cached** badge, Retrieval Debug
does the same for searches, and Settings has a cache panel with a **Clear cache** button. The counters and `index_version`
live in keys outside the cache prefixes, so `cache-clear` doesn't reset them.

### Try it: the cache in action

1. **Ask twice.** Use a request with no conversation id, so there is no history:
   ```powershell
   $q = @{ question = "What does error code E-271 mean?" } | ConvertTo-Json
   $a = Invoke-RestMethod -Method Post -ContentType application/json -Body $q http://localhost:8000/ask
   $b = Invoke-RestMethod -Method Post -ContentType application/json -Body $q http://localhost:8000/ask
   $a.cached, $b.cached, $a.latency_ms, $b.latency_ms     # False, True, and the second is far faster
   ```
   Open `$a.trace_url` and `$b.trace_url` in Langfuse. The first is the full graph (rewrite, retrieval, grade, answer, grounding).
   The second is a short `ask` trace tagged **`cache:hit`** with no LLM spans. (In the UI, ask the same question in a **New chat**:
   in the same chat the earlier messages count as history, so it is not cached.)
2. **Statistics.** `.\tasks.ps1 cache-stats`: the `answer` kind shows 1 hit, 1 miss and 1 key; `embedding` and `search` have keys too.
3. **A new document invalidates answers.** Note `index_version` in `cache-stats`, upload any small `.md` on the Documents page
   (or `.\tasks.ps1 ingest` after adding a file), and check that `index_version` went up. Ask the same question again:
   `cached` is `False`. The old answer is still in Redis, but nothing matches its key any more.
4. **Look at the keys.** The password is in `.env` (`REDIS_PASSWORD`):
   ```powershell
   podman exec rag-redis redis-cli -a <password> --no-auth-warning --scan
   podman exec rag-redis redis-cli -a <password> --no-auth-warning ttl "answer:<paste a key from the list>"
   ```
5. **Bypass and clear.** Send the question with `use_cache = $false` in the body and see `cached` is `False`.
   `.\tasks.ps1 cache-clear answer` empties only the answers.
6. **Fail-open.** `podman stop rag-redis`, then ask something new. It still answers (slower, no cache), `/health` shows `redis` unreachable
   and `status` is still `ok`. `podman start rag-redis` brings the cache back after at most 10 s.

## What to check

- [ ] `gethostbyname('mongodb')` fails in `rag-frontend` and works in `rag-backend`.
- [ ] The Logs page shows `ui` rows, although the frontend has no Mongo settings.
- [ ] The second identical question has `cached: True` and a `cache:hit` trace with no LLM spans.
- [ ] After an upload, the same question is answered fresh again.
- [ ] You can explain why Langfuse's Redis and the app's Redis are two containers, and why follow-ups are never answer-cached.
