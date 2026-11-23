"""Turn .env into the Kubernetes Secrets for `podman kube play` (deploy/kube/secret.yaml).

Two secrets, so each pod only gets what it needs (MASTER_PLAN §15.4):
  backend-secrets   everything in .env except the values the pod's ConfigMap sets itself
  frontend-secrets  only API_KEY and MCP_API_KEY (no model keys, no database settings)

The output contains your API keys, so it is git-ignored.
"""
import base64
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# set by the backend ConfigMap in backend.yaml, so they must not also come from the secret
BACKEND_SKIP = {"MONGO_URI", "QDRANT_URL", "REDIS_URL", "BACKEND_URL", "DOCS_DIR", "LANGFUSE_HOST",
                "LANGFUSE_PUBLIC_URL", "MCP_API_KEY"}
FRONTEND_KEYS = ["API_KEY", "MCP_API_KEY"]


def secret(name: str, pairs: list[tuple[str, str]]) -> str:
    lines = ["apiVersion: v1", "kind: Secret", "metadata:", f"  name: {name}", "type: Opaque", "data:"]
    lines += [f"  {k}: {base64.b64encode(v.strip().encode()).decode()}" for k, v in pairs]
    return "\n".join(lines) + "\n"


env = (ROOT / ".env").read_text(encoding="utf-8")
all_pairs = re.findall(r"^([A-Z0-9_]+)=(.*)$", env, re.M)
backend = [(k, v) for k, v in all_pairs if k not in BACKEND_SKIP]
values = dict(all_pairs)
frontend = [(k, values.get(k, "")) for k in FRONTEND_KEYS]

out = Path(__file__).with_name("secret.yaml")
out.write_text(secret("backend-secrets", backend) + "---\n" + secret("frontend-secrets", frontend),
               encoding="utf-8")
print(f"wrote {out.relative_to(ROOT)}: backend-secrets ({len(backend)} keys), "
      f"frontend-secrets ({len(frontend)} keys)  [git-ignored]")
