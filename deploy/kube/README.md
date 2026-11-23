# Kubernetes YAML for `podman kube play`

```
Phase 10: the app stack as Kubernetes YAML, run with `podman kube play`.
Same two tiers as compose.yaml (MASTER_PLAN §15.6):

  pod "backend"   backend + mongodb + qdrant + redis   (networks: frontend-net, backend-net)
  pod "frontend"  frontend + mcp-server                (network: frontend-net)

  python deploy/kube/make_secret.py                    # .env -> deploy/kube/secret.yaml (git-ignored)
  podman kube play deploy/kube/secret.yaml
  podman kube play --network frontend-net:alias=backend --network backend-net deploy/kube/backend.yaml
  podman kube play --network frontend-net deploy/kube/frontend.yaml
  (tasks.ps1 kube-up does all of it; kube-down stops both pods, volumes are kept)

This file only documents the layout; the runnable YAML is split in backend.yaml and frontend.yaml
because `podman kube play` attaches ONE set of networks to everything in a file.

Differences from compose.yaml (this is the lesson):
- Containers in one pod share a network namespace, so inside the backend pod they reach each
  other on localhost (mongodb://localhost:27017). The frontend pod reaches the backend pod by
  the network alias `backend` on frontend-net.
- Named volumes become PersistentVolumeClaims; podman creates them as local volumes.
- The data/docs folder is a hostPath. Paths are inside the Podman VM, where your C: drive
  is mounted at /mnt/c.
- Secrets come from Kubernetes Secrets (envFrom.secretRef): the frontend pod only gets
  API_KEY / MCP_API_KEY, never the Gemini / Hugging Face keys or database settings.
Stop the compose app first (tasks.ps1 kube-up does): the host ports are the same.
```

## Isolation caveat (tested)

All containers of a pod share one network namespace. The `backend` pod is attached to `frontend-net` (so the
frontend can reach the backend), which also puts its `mongodb`, `qdrant` and `redis` containers on that network:
from the frontend pod they resolve and their ports are reachable. Under `compose.yaml` the data stores are on
`backend-net` only and the frontend cannot resolve them (checked by `tests/integration/test_stack.py`).
In Kubernetes you would add a NetworkPolicy; with plain pods, move the data stores into a third pod.
