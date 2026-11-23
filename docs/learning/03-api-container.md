# 03 — The backend container: writing a Containerfile

Files: [backend/Containerfile](../../backend/Containerfile),
[backend/.containerignore](../../backend/.containerignore),
[backend/pyproject.toml](../../backend/pyproject.toml),
[main.py](../../backend/backend_app/main.py), [config.py](../../backend/backend_app/config.py).
(Image `localhost/hybrid-rag-backend`, container `rag-backend`, compose service `backend`. The Python
package is `backend_app`, so a log line or an import tells you which tier it belongs to.)

A `Containerfile` is Podman's name for a `Dockerfile`. The syntax is the same.

## Why build it this way

Three goals: **fast rebuilds** when you change code, a **small and safe** runtime image, and
**no secrets** inside the image. The frontend, MCP and eval Containerfiles follow the same pattern.

## Line by line

```dockerfile
# ---------- stage 1: build a virtualenv with all dependencies ----------
FROM docker.io/library/python:3.12-slim AS builder
```
Stage 1 is a throwaway "builder". `AS builder` names it so stage 2 can copy from it.

```dockerfile
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /build
```
Everything gets installed into one folder, `/opt/venv`, so it can be copied as a whole.
No pip cache means fewer wasted megabytes.

```dockerfile
COPY pyproject.toml .
RUN python -c "import tomllib; ... write requirements.txt" \
 && pip install -r requirements.txt
```
This is the **layer caching** trick. Each instruction produces a *layer*, and Podman reuses a
cached layer when the instruction and its input files are unchanged. Only `pyproject.toml` is
copied before the slow `pip install`, so editing `backend_app/*.py` does not invalidate the
install layer. The rebuild then takes seconds instead of minutes. The inline Python just turns
`[project].dependencies` into a `requirements.txt` (the versions are pinned in `pyproject.toml`).

```dockerfile
# ---------- stage 2: small runtime image ----------
FROM docker.io/library/python:3.12-slim
RUN useradd --create-home --uid 10001 app
COPY --from=builder /opt/venv /opt/venv
```
**Multi-stage build.** The final image starts fresh and takes only the finished venv from
stage 1. Build leftovers stay behind in the builder: `/build`, `requirements.txt`, pip's temp files,
and compilers if you ever add them. A non-root user `app` (uid 10001) is created.

```dockerfile
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY backend_app ./backend_app
```
`PYTHONUNBUFFERED=1` makes `print`/log lines show up in `podman logs` immediately.
The application code is copied **last**, because it changes most often.

```dockerfile
USER app
EXPOSE 8000
CMD ["uvicorn", "backend_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
- `USER app`: the server runs **as a non-root user**. If someone breaks into the process, they
  can't change system files in the container.
- `EXPOSE` is documentation only. The real publishing is `ports:` in compose.
- `--host 0.0.0.0`: listen on all interfaces *inside the container*. With `127.0.0.1`, other
  containers and the port mapping couldn't reach it.

## `.containerignore`

It works like `.gitignore`, but for the **build context** (the folder sent to the builder):
`__pycache__`, `*.pyc`, `*.egg-info`, `.pytest_cache`, `.env`, `.env.*`. The context is
`./backend`, so the root `.env` isn't in it anyway. Ignoring `.env*` is a second safety
net in case someone ever copies a `.env` into that folder.

## Runtime secrets: never in the image

`HF_TOKEN`, `GOOGLE_API_KEY` and the Langfuse keys are **not** in the Containerfile. Compose passes them
when the container is created:

```yaml
backend:
  env_file: [.env]           # all keys from .env become environment variables
  environment:               # these override .env for this container
    DOCS_DIR: /data/docs
    MONGO_URI: mongodb://mongodb:27017
    QDRANT_URL: http://qdrant:6333
    REDIS_URL: redis://:${REDIS_PASSWORD:-}@redis:6379/0
```

The backend is the only container that gets the whole `.env`. The frontend, MCP server and eval
get just the few variables listed under their own `environment:` (see [12](12-frontend-backend-and-cache.md)).
Compose does not expand `${...}` inside `.env` values, so the backend itself fills in the password
when it reads `REDIS_URL` (see [troubleshooting](troubleshooting.md#9-env-values-with--are-not-expanded)).

An image can be shared, pushed to a registry or inspected with `podman history`. Anything
baked into it should be treated as public. Settings are read by pydantic-settings in
[config.py](../../backend/backend_app/config.py): the field `hf_token` reads env var `HF_TOKEN`
(not case sensitive).

## `/health`: what each check means

`GET /health` runs six checks ([main.py](../../backend/backend_app/main.py)):

| Check | Code | What "ok" means |
|---|---|---|
| `mongodb` | `mongo.ping()` | Mongo answers `ping` |
| `qdrant` | `qdrant_store.ping()` | Qdrant lists collections; the detail shows the point count |
| `redis` | `cache.ping()` | The app cache answers `PING` (or says it is disabled, see [12](12-frontend-backend-and-cache.md)) |
| `embeddings` | `check_embeddings()` | A tiny embed call returns a vector of `EMBEDDING_DIM` (384). **Cached 5 minutes** to save HF quota |
| `llm` | `check_llm()` | Only checks the provider is configured. **No API call**, so it saves Gemini quota |
| `langfuse` | `check_langfuse()` | Keys set and `auth_check()` succeeds |

`status` is `ok` when Mongo **and** Qdrant are ok, `degraded` otherwise. The other four can be red
and the backend still serves requests. `bm25_chunks` shows how many chunks the in-memory BM25 index holds.

The **container** healthcheck in compose calls `/config`, not `/health`. A healthcheck runs
every 15 s. It should only prove the process is up and must not spend HF quota or fail
because Langfuse is restarting.

## Working without API keys: the fake providers

Set in `.env` (each on its own line, no inline comments):

```
LLM_PROVIDER=fake
EMBEDDING_PROVIDER=fake
```

- `FakeEmbeddings` ([providers.py](../../backend/backend_app/embeddings/providers.py)) turns words
  into deterministic hash vectors. Similar wording gives similar vectors, but there is no real meaning.
- `FakeRAGChatModel` ([llm/client.py](../../backend/backend_app/llm/client.py)) recognises each
  step from the `TASK:` line of the prompt. It marks every excerpt relevant, quotes excerpt `[1]`
  as the answer, and always says "grounded".

The whole pipeline runs, with no network and no cost. Unit tests use the same fakes ([tests/conftest.py](../../tests/conftest.py)).
Switching back to real providers is in [04](04-ingestion.md#switching-from-fake-to-real-embeddings).

## Try it

```powershell
.\tasks.ps1 rebuild backend                  # build + recreate only the backend container
podman history localhost/hybrid-rag-backend:latest   # the layers and their sizes
podman image ls localhost/hybrid-rag-backend     # ~430 MB with all of LangChain/LangGraph
podman exec rag-backend whoami                   # app  (not root)
podman exec rag-backend python -c "import os; print('HF_TOKEN' in os.environ)"   # True: in the *container*...
podman image inspect localhost/hybrid-rag-backend:latest --format "{{.Config.Env}}"  # ...not in the image
Invoke-RestMethod http://localhost:8000/health | ConvertTo-Json -Depth 4
Invoke-RestMethod -Method Post http://localhost:8000/embed-test -ContentType application/json -Body '{}'
```

Now change one line in `backend/backend_app/main.py` (a comment is enough) and rebuild. The build
output says the `pip install` step came from the cache. Then change `pyproject.toml`
and watch it reinstall everything.

API docs (Swagger UI) are at http://localhost:8000/docs.

## What to check

- [ ] `/embed-test` returns `"dim": 384`.
- [ ] `whoami` in the container prints `app`.
- [ ] `HF_TOKEN` exists in the running container but not in `podman image inspect`.
- [ ] A code-only rebuild reuses the cached dependency layer.
