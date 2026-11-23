# 10 — Pods and Kubernetes YAML with `podman kube play`

Files: [deploy/kube/backend.yaml](../../deploy/kube/backend.yaml),
[deploy/kube/frontend.yaml](../../deploy/kube/frontend.yaml), [deploy/kube/README.md](../../deploy/kube/README.md),
[deploy/kube/make_secret.py](../../deploy/kube/make_secret.py), `kube-up` / `kube-down` in [tasks.ps1](../../tasks.ps1).

## Why

Compose is a local tool. Kubernetes (K8s) is how containers run on servers, and it speaks its own YAML:
Pods, Volumes, ConfigMaps, Secrets. `podman kube play` runs that same YAML on your PC, with no
cluster. You learn the K8s vocabulary on a stack you already understand.

## A pod = containers that share one network namespace

In compose, each container has its own IP and reaches the others **by name** (`mongodb`).
In a **pod**, all containers share one network namespace, held by a small *infra* container:
same IP, same `localhost`. So inside the `backend` pod the ConfigMap says:

```yaml
MONGO_URI: mongodb://localhost:27017      # compose: mongodb://mongodb:27017
QDRANT_URL: http://localhost:6333         # compose: http://qdrant:6333
REDIS_URL: redis://:${REDIS_PASSWORD}@localhost:6379/0    # compose: ...@redis:6379/0
```

A side effect: two containers in one pod can't use the same port. That's fine here, since every
service has its own port.

## Two pods, the same two tiers as compose

| Pod | File | Containers | Networks |
|---|---|---|---|
| `backend` | `backend.yaml` | backend, mongodb, qdrant, redis | `frontend-net` (with alias `backend`) and `backend-net` |
| `frontend` | `frontend.yaml` | frontend, mcp-server | `frontend-net` only |

The frontend pod reaches the backend pod as `http://backend:8000` (`BACKEND_URL` in its ConfigMap).
That works because the backend pod is started with a **network alias**: `--network frontend-net:alias=backend`.
**Isolation is weaker here than in compose** (tested, not assumed): all containers of a pod share ONE network
namespace, so putting the backend pod on `frontend-net` puts `mongodb`, `qdrant` and `redis` on it too. From the
frontend pod, `mongodb`, `qdrant` and `redis` DO resolve (and their ports are reachable), unlike under compose,
where each container has its own network attachments and the frontend cannot resolve them. Real Kubernetes has the
same property and fixes it with a NetworkPolicy. To get compose-level isolation with pods, put the data stores in a
third pod on `backend-net` only (the backend then reaches them by pod/alias name instead of `localhost`).
Why two files? `podman kube play` attaches **one** set of networks to everything in a file, and the two pods need different sets.
This mirrors a real cluster, where each tier is its own Deployment behind a Service.

## The pieces of the YAML

| Kind | Name | Compose equivalent |
|---|---|---|
| `PersistentVolumeClaim` | `kube-mongo-data`, `kube-qdrant-storage`, `kube-redis-data` | named volumes `mongo_data`, `qdrant_storage`, `redis_data` |
| `ConfigMap` | `backend-config`, `frontend-config` | the `environment:` blocks (non-secret settings) |
| `Secret` | `backend-secrets`, `frontend-secrets` (from `secret.yaml`) | `env_file: [.env]` (backend) / the few keys the frontend gets |
| `Pod` | `backend` (4 containers), `frontend` (2 containers) | the 6 app services |
| `hostPath` volumes | `data/docs`, `data/qdrant_snapshots` | bind mounts `./data/docs`, `./data/qdrant_snapshots` |

**PVCs.** A claim says "I need storage". Podman satisfies it with a local named volume
of the same name (`podman volume ls` shows `kube-mongo-data`). These are **different volumes** from
compose's `hybrid-rag_mongo_data`, so the pod starts with an empty Mongo and Qdrant. The backend
re-ingests `data/docs` on startup, so documents come back. Chat history and eval runs from the
compose stack don't. They are still in the compose volumes.

**hostPath.** The path is a path **inside the Podman VM**, where your C: drive is `/mnt/c`:
`/mnt/c/Users/mbdin/Desktop/hybrid-rag/data/docs`. It's hard-coded, so edit the `path:` lines in
`backend.yaml` if you move the repo. `type: Directory` makes the pod fail if the folder doesn't exist.

**ConfigMap vs Secret.** Both turn into environment variables through `envFrom`. A ConfigMap is
for plain settings. A Secret is for keys, and K8s handles it more carefully (separate access rights,
not shown in `describe`). Its values are **base64-encoded, not encrypted**: anyone who can read the
file can decode them. [make_secret.py](../../deploy/kube/make_secret.py) reads `.env` and writes
`deploy/kube/secret.yaml` (git-ignored) with **two** secrets, so each pod gets only what it needs:

- `backend-secrets`: everything in `.env` except the keys the backend ConfigMap sets itself (`MONGO_URI`,
  `QDRANT_URL`, `REDIS_URL`, `BACKEND_URL`, `DOCS_DIR`, `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_URL`) and `MCP_API_KEY`.
- `frontend-secrets`: only `API_KEY` and `MCP_API_KEY`. The frontend pod never sees the Gemini / Hugging Face keys.

(`REDIS_URL` contains `${REDIS_PASSWORD}`. Nothing expands it in K8s YAML either, so the backend resolves it itself,
as in compose. See [troubleshooting](troubleshooting.md#9-env-values-with--are-not-expanded).)

**Probes.** The `backend` container has a `readinessProbe` (`GET /config` on 8000), the K8s version of
a healthcheck. There is no `depends_on` in K8s. Containers start without waiting for each other.
The backend copes on its own: its startup thread retries the Mongo/Qdrant connection 30 times, 2 s apart, and the
cache is fail-open if Redis is not up yet.

## Reaching Langfuse: `backend-net`

Langfuse stays in compose. The backend pod joins `backend-net`, where `langfuse-web` lives, so it can resolve
`langfuse-web` by name (`LANGFUSE_HOST: http://langfuse-web:3000`).
That is why Langfuse must be running first (`.\tasks.ps1 up-obs`).

## Running it

```powershell
.\tasks.ps1 kube-up
```

That runs, in order:

1. `podman-compose -f compose.yaml down`: stop the compose app (same ports). Langfuse keeps running.
   The warning that `backend-net` can't be removed because Langfuse still uses it is harmless.
2. `podman network create frontend-net` and `backend-net` (ignored if they exist).
3. `python deploy/kube/make_secret.py`: `.env` → `secret.yaml` (two secrets).
4. `podman kube play --replace deploy/kube/secret.yaml`: creates the Podman secrets.
5. `podman kube play --replace --network frontend-net:alias=backend --network backend-net deploy/kube/backend.yaml`: PVCs, ConfigMap, backend pod.
6. `podman kube play --replace --network frontend-net deploy/kube/frontend.yaml`: ConfigMap, frontend pod.

Back to compose:

```powershell
.\tasks.ps1 kube-down      # podman kube down (frontend, backend, secret), then compose up for the app again
```

`kube down` removes the pods but keeps the PVC volumes.

## How this maps to real Kubernetes

| Here | On a real cluster |
|---|---|
| Two pods, 4 + 2 containers | Usually one **Deployment** (or StatefulSet for databases) per service, each with its own pods |
| `localhost` inside a pod, a network alias between pods | A **Service** per component, giving DNS names like `backend` and `mongodb` |
| Two Podman networks | **NetworkPolicies** that only let the frontend reach the backend |
| `hostPort` + `hostIP: 127.0.0.1` | A Service of type LoadBalancer/NodePort, or an **Ingress** |
| PVC → local Podman volume | PVC → a disk from a **StorageClass** (cloud disk, NFS, ...) |
| `hostPath` to `/mnt/c/...` | Avoided: pods move between nodes. Docs would go in a PVC or object storage |
| `localhost/hybrid-rag-backend:latest` | An image pushed to a **registry** that the cluster can pull from, with a version tag |
| Secrets from `make_secret.py` | `kubectl create secret`, or an external secret manager |

## Try it

1. `.\tasks.ps1 up-obs`, then `.\tasks.ps1 kube-up`.
2. Look at the pods:
   ```powershell
   podman pod ps                          # backend (4 + infra) and frontend (2 + infra), Running
   podman ps --pod --filter pod=backend
   podman logs backend-backend --tail 20  # pod containers are named <pod>-<container>
   podman volume ls | Select-String kube-
   ```
3. Prove the tiers: the **frontend** pod reaches the backend by its alias, but cannot see the data stores:
   ```powershell
   podman exec frontend-frontend python -c "import urllib.request; print(urllib.request.urlopen('http://backend:8000/config').status)"
   podman exec frontend-frontend python -c "import socket; print(socket.gethostbyname('mongodb'))"   # fails
   ```
   And prove the shared namespace inside the backend pod: `podman exec backend-backend python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:6333/').status)"` reaches Qdrant on `localhost`.
4. `.\tasks.ps1 kube-down` and check that the compose app is back: `.\tasks.ps1 status`.
5. Go the other way: generate K8s YAML from a running compose container. It only prints YAML:
   `podman kube generate rag-qdrant`. (Pick a container without an `env_file`. For the others,
   the generated YAML contains their env vars, secrets included.)

## What to check

- [ ] http://localhost:8050 works while the app runs as two pods.
- [ ] `/health` shows `langfuse: ok`, so the backend pod reaches `langfuse-web` over `backend-net`.
- [ ] The frontend pod resolves `backend` but not `mongodb`.
- [ ] You can explain why the ConfigMap uses `localhost` but compose uses service names.
- [ ] You can name two things that would change on a real cluster (registry, Services, StorageClass, no hostPath).
