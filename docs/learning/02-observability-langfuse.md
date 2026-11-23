# 02 — Observability: the Langfuse stack

Files: [compose.observability.yaml](../../compose.observability.yaml),
[scripts/gen_langfuse_env.py](../../scripts/gen_langfuse_env.py), [tracing.py](../../backend/backend_app/tracing.py).

## Why

A RAG answer is the result of many steps: rewrite, two searches, fusion, grading, generation
and a grounding check. When an answer is wrong you need to see **which step** went wrong.
Langfuse records each request as a **trace** with nested **spans** (one per step) and
**generations** (LLM calls with prompt, output, tokens and latency).

It is also the best containerization lesson in the project: a real product made of six
containers with their own databases, queue and object store.

## Version note

The plan says Langfuse v3. The code runs **v4** (`docker.io/langfuse/langfuse:4` and
`langfuse-worker:4`), because the Python SDK we use (`langfuse==5.2.0`, pinned in
[backend/pyproject.toml](../../backend/pyproject.toml)) needs a Langfuse server ≥ 4.59.

## The six containers

| Container | Role | Published port |
|---|---|---|
| `langfuse-web` | UI + public API the SDK sends traces to | `127.0.0.1:3000` |
| `langfuse-worker` | Background jobs: takes events from the queue and writes them to ClickHouse | none |
| `langfuse-postgres` | Metadata: users, projects, API keys, datasets, prompts | none |
| `langfuse-clickhouse` | Analytics store for traces, observations and scores | none |
| `langfuse-redis` | Queue + cache between web and worker | none |
| `langfuse-minio` | S3-compatible blob store: raw events and media | `127.0.0.1:9090` → MinIO's S3 port 9000 |

**Internal-only services** have no `ports:` section. Postgres, ClickHouse and Redis can be reached
only from other containers on `langfuse-net` (as `langfuse-postgres:5432` and so on), not from your PC
and not from the app containers. That network is `internal: true`: it has no route out of the stack.
Only `langfuse-web` also joins `backend-net`, so the backend can send traces and you can open :3000.
(The app has its own, separate `redis` container, see [12](12-frontend-backend-and-cache.md).)
That is fewer open doors, and nothing on Windows can clash with their ports. MinIO is published
only because the browser loads media through `LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT=http://localhost:9090`.
(The plan calls 9090 the MinIO *console*. In the code it is the S3 API. The console port 9001 is not published.)

## Start order: `depends_on` with `service_healthy`

```yaml
langfuse-worker:
  depends_on: &lf-deps            # "&lf-deps" names this block (a YAML anchor)
    langfuse-postgres:   { condition: service_healthy }
    langfuse-minio:      { condition: service_healthy }
    langfuse-redis:      { condition: service_healthy }
    langfuse-clickhouse: { condition: service_healthy }
langfuse-web:
  depends_on: *lf-deps            # "*lf-deps" reuses the same block
```

Plain `depends_on` only waits until a container has *started*. `condition: service_healthy`
waits until its healthcheck *passes*. Web and worker run database migrations on start, so
they must not start before Postgres and ClickHouse accept connections.

Two details in the file worth reading:

- `langfuse-web` sets `HOSTNAME: 0.0.0.0`. Next.js listens on `$HOSTNAME`, and without this the
  healthcheck on `127.0.0.1` is refused (see [troubleshooting](troubleshooting.md#3-langfuse-web-stays-starting)).
- Redis gets its password with `$$REDIS_AUTH`. `$$` is an escaped `$`: compose leaves it alone, and
  the shell inside the container expands it from the container's environment.

## Secrets in env files, and headless init

`.env.langfuse` holds the server secrets: `NEXTAUTH_SECRET`, `SALT`, `ENCRYPTION_KEY`, DB/Redis/MinIO
passwords. Every Langfuse container reads it with `env_file: [.env.langfuse]`. It is git-ignored.
Nobody types these values. The generator writes them:

```powershell
.\.venv\Scripts\python.exe scripts\gen_langfuse_env.py    # also run by .\tasks.ps1 setup
```

It also writes the **headless init** variables `LANGFUSE_INIT_*`. On the very first start,
Langfuse uses them to create the org (`local-org`), the project (`hybrid-rag`), the project's
API keys and an admin user. The script then copies the public/secret key and project id into
`.env` (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_PROJECT_ID`) so the backend can send traces.

Re-running the script keeps the existing file. `--force` makes new secrets, and then the
existing Postgres data can no longer be decrypted. Use it only on a fresh install, with the volumes deleted.

## Multiple compose files

The app and Langfuse live in separate files, but both declare `name: hybrid-rag` and the
same `backend-net` network (Langfuse adds its own internal `langfuse-net`):

```powershell
.\tasks.ps1 up-obs       # only Langfuse:  podman-compose -f compose.observability.yaml up -d
.\tasks.ps1 up-app       # only the app
.\tasks.ps1 up           # both: -f compose.yaml -f compose.observability.yaml
```

## Logging in and finding things

```powershell
Select-String -Path .env.langfuse -Pattern "LANGFUSE_INIT_USER_(EMAIL|PASSWORD)"
```

Open http://localhost:3000 and log in with that email (`admin@example.com`) and password. In the project **Hybrid RAG**:

- **Tracing → Traces**: one row per request, named `ask`, `search` or `ingest`. Filter by tags
  such as `source:ui`, `source:mcp`, `mode:bm25`, `trigger:startup`, `cache:hit`.
- **Sessions**: one per chat conversation (`session_id` = `conversation_id`).
- **Scores**: `grounded`, `user_feedback` (👍/👎), and `hit_at_k`, `mrr`, `ragas_*` from evaluation.
- **Datasets**: `rag-golden`, with its dataset runs (the SDK calls them *experiments*, see [07](07-evaluation-ragas.md)).

Each answer in the UI has an *Open trace* link built by `trace_url()` in
[tracing.py](../../backend/backend_app/tracing.py): `http://localhost:3000/project/hybrid-rag/traces/<id>`.

Tracing is **fail-open**. If keys are missing or Langfuse is down, `get_langfuse()` returns
`None`, and the helpers do nothing while the RAG keeps answering.

## Try it

1. Watch the start order: `.\tasks.ps1 up-obs`, then run `podman ps` a few times. The
   databases turn `(healthy)` first, then web and worker start. `langfuse-worker` has no
   healthcheck, so it shows only `Up`.
2. Resource use: `podman stats --no-stream`. ClickHouse and langfuse-web are usually the
   biggest. If the machine runs short of memory, raise `memory=` in `%UserProfile%\.wslconfig`,
   then run `wsl --shutdown` and `podman machine start`.
3. Prove an internal-only service is internal:
   `Test-NetConnection localhost -Port 5432` fails from Windows, but
   `podman exec rag-langfuse-postgres pg_isready -U postgres` works inside.
4. Send a trace yourself (the project venv already has the `langfuse` SDK):
   ```powershell
   $env:LANGFUSE_PUBLIC_KEY = (Select-String .env -Pattern '^LANGFUSE_PUBLIC_KEY=(.*)').Matches.Groups[1].Value
   $env:LANGFUSE_SECRET_KEY = (Select-String .env -Pattern '^LANGFUSE_SECRET_KEY=(.*)').Matches.Groups[1].Value
   @'
   from langfuse import Langfuse
   lf = Langfuse(base_url="http://localhost:3000")   # keys come from the env vars above
   with lf.start_as_current_observation(name="hello-from-powershell", as_type="span") as span:
       span.update(output="hi")
   lf.flush()
   print("sent")
   '@ | .\.venv\Scripts\python.exe -
   ```
   (The `'@` that closes the here-string must be at the start of its line, with no spaces before it.
   Remove the indentation when you paste.)
   Use `http://localhost:3000` here: `langfuse-web` is a name that only resolves on `backend-net`.
   The trace `hello-from-powershell` shows up in **Traces** after a few seconds.

## What to check

- [ ] `podman ps` shows all six `rag-langfuse-*` containers; web, postgres, clickhouse, redis and minio are `(healthy)`.
- [ ] `Invoke-RestMethod http://localhost:8000/health` shows `langfuse: ok`.
- [ ] You can log in at http://localhost:3000 and see `ask` / `ingest` traces.
- [ ] You can explain the difference between `depends_on` and `depends_on: condition: service_healthy`.
