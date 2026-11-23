# 09 — Compose everything: one command for the whole system

Files: [compose.yaml](../../compose.yaml), [compose.observability.yaml](../../compose.observability.yaml),
[tasks.ps1](../../tasks.ps1), [.env.example](../../.env.example).

## Why compose

Starting 12 containers by hand with `podman run` means 12 long commands with ports, volumes,
networks, env files and the right order. A compose file **declares** all of that once. Then
`up` makes reality match the file, and `down` removes the containers again (but not the volumes).

## Two files, one project

| File | Services |
|---|---|
| `compose.yaml` | `frontend`, `mcp-server` (frontend tier); `backend`; `mongodb`, `qdrant`, `redis` (data); plus `eval` (profile `eval`) and `mongo-express` (profile `dev`). The file is grouped into FRONTEND / BACKEND / DATA / JOBS / DEV sections |
| `compose.observability.yaml` | `langfuse-web`, `langfuse-worker`, `langfuse-postgres`, `langfuse-clickhouse`, `langfuse-redis`, `langfuse-minio` |

Both declare `name: hybrid-rag` and the shared network `backend-net` with a fixed `name:`.
`compose.yaml` adds `frontend-net`; `compose.observability.yaml` adds the internal `langfuse-net`.
Passing both with `-f` merges them into one project. Used alone, each still joins `backend-net`, so
Langfuse can run all day while you restart the app. Which container is on which network is in
[12](12-frontend-backend-and-cache.md).

```powershell
.\.venv\Scripts\podman-compose.exe -f compose.yaml -f compose.observability.yaml config   # the merged result
```

You may notice `pod_hybrid-rag` in `podman pod ls`. podman-compose groups the project's containers into a pod
by default. That pod has no infra container and shares no namespaces, so it is only a label here.
Real pods are in [10](10-pods-kube.md).

## Profiles

A service with `profiles: [...]` is **skipped** unless you ask for that profile:

| Profile | Service | Start it with |
|---|---|---|
| `dev` | `mongo-express` (http://localhost:8081) | `.\tasks.ps1 dev-tools` |
| `eval` | `eval` (one-off job) | `.\tasks.ps1 eval --mode all --no-ragas` |

This keeps dev tools and jobs in the same file without starting them on every `up`.

## Restart policies

Every long-running service has `restart: unless-stopped`: if the process crashes, Podman starts it again.
`eval` has no restart policy, because a job that finished must stay finished.
After a Windows reboot or `podman machine stop`, start the machine and run `.\tasks.ps1 up`. It's quick, and it is safe
to run when things are already up.

## Everyday tasks

| You want to... | Command | What happens |
|---|---|---|
| Start everything | `.\tasks.ps1 up` | `up -d` with both files |
| Start only the app / only Langfuse | `.\tasks.ps1 up-app` / `up-obs` | One file |
| See state + health | `.\tasks.ps1 status` | `podman ps -a` + the backend's `/health` checks |
| Follow logs | `.\tasks.ps1 logs backend` | `logs -f backend` (Ctrl+C to stop). Also `frontend`, `mcp-server`, `redis`, ... |
| Apply a **code** change | `.\tasks.ps1 rebuild backend` | `build backend`, then `up -d --force-recreate --no-deps backend` |
| Apply an **`.env`** change | `.\tasks.ps1 restart backend` | `up -d --force-recreate --no-deps backend` |
| Stop everything | `.\tasks.ps1 down` | Removes all containers. **Volumes stay** |
| Re-scan documents | `.\tasks.ps1 ingest` (or `ingest force` = re-embed everything) | `POST /ingest/scan` |
| Look at / empty the cache | `.\tasks.ps1 cache-stats` / `cache-clear [kind]` | `GET /admin/cache/stats`, `POST /admin/cache/clear` |
| Restore a Qdrant snapshot | `.\tasks.ps1 qdrant-restore [name]` | no name = list snapshots |
| Unit / integration tests | `.\tasks.ps1 test` / `itest` | pytest on the host |

Two flags in `rebuild` / `restart` matter:

- `--force-recreate`: make a **new** container even if compose thinks nothing changed.
- `--no-deps`: don't touch the services it depends on. Otherwise recreating `backend` could also restart Mongo and Qdrant.

### Why `podman restart` is not enough after editing `.env`

A container's environment is fixed **when the container is created**. `podman restart rag-backend`
restarts the same container with the **old** environment. You need a new container:
`.\tasks.ps1 restart backend`. The same goes for changes to `ports:`, `volumes:` or `environment:` in compose.
For code changes you need a new **image** first, which is why `rebuild` runs `build` before `up`.

Which services read `.env`? Only the `backend` (`env_file: [.env]`). The `frontend`, `mcp-server` and `eval`
get just the few variables listed under their own `environment:` block (`${VAR:-default}` is filled from `.env` by
compose itself), so each container holds only the secrets it needs. The Langfuse containers read `.env.langfuse`.
After editing `.env`, `.\tasks.ps1 setup` (it runs `scripts/sync_env.py`) adds any new keys without touching your values.

## Env files must not have inline `# comments`

```
LLM_THINKING_BUDGET=0      # off        ← wrong: the value becomes "0      # off"
# 0 = off                               ← right: comments on their own line
LLM_THINKING_BUDGET=0
```

The env-file format has no inline comments. Everything after `=` is the value, so pydantic then
fails to parse `"0      # off"` as an int, or a key gets a trailing `# ...` that silently breaks it.
[.env.example](../../.env.example) says this at the top and keeps every comment on its own line.

## Try it

1. **From zero, with data kept:**
   ```powershell
   .\tasks.ps1 down
   .\tasks.ps1 up
   .\tasks.ps1 status        # repeat until everything is (healthy); langfuse-worker just shows Up
   Invoke-RestMethod http://localhost:8000/stats    # same documents/chunks as before
   ```
2. **Rebuild one service:** edit a string in `frontend/frontend_app/pages/settings.py`, run
   `.\tasks.ps1 rebuild frontend`, and check that `podman ps` shows only `rag-frontend` as freshly created.
3. **`.env` change:** set `TOP_K_FINAL=3` in `.env`. Run `podman restart rag-backend`, then
   `Invoke-RestMethod http://localhost:8000/config | Select-Object top_k_final`: still 5.
   Now `.\tasks.ps1 restart backend` and check again: 3. Set it back to 5 and recreate once more.
4. **Profiles:** `.\tasks.ps1 dev-tools`, then `podman ps --filter name=mongo-express`. Remove it again with
   `podman rm -f rag-mongo-express` (the next plain `up` won't start it).

## What to check

- [ ] `.\tasks.ps1 up` from a stopped state brings back all 12 containers with data intact.
- [ ] You know when to use `rebuild` (code) vs `restart` (env/config).
- [ ] `.env` has no `KEY=value  # comment` lines.
- [ ] You can say which service reads the whole `.env`, and which ones read `.env.langfuse`.
