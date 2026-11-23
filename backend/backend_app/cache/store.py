"""Cache operations on top of Redis (MASTER_PLAN §15.3).

Four kinds, each with its own key prefix so they can be counted and cleared separately:

  embedding  emb:{provider}:{model}:{sha256(text)}          float32 bytes    30 days
  search     search:{index_version}:{sha256(parts)}         JSON             10 min
  answer     answer:{index_version}:{sha256(parts)}         JSON             24 h
  health     health:{check}                                 JSON             5 min

`index_version` is a counter bumped whenever the indexed chunks change, so search and answer
entries made before an upload are never matched again (they expire on their own).
"""
from __future__ import annotations

import hashlib
import json
from array import array
from contextvars import ContextVar

import redis as redis_lib

from backend_app.cache import redis_client
from backend_app.config import get_settings

KINDS = ("embedding", "search", "answer", "health")
PREFIX = {"embedding": "emb:", "search": "search:", "answer": "answer:", "health": "health:"}
INDEX_VERSION_KEY = "cache:index_version"
STATS_PREFIX = "cache:stats:"

# Per-request switch (set by ask() / search()) so one request can bypass the cache without
# changing function signatures. Contexts are copied into LangGraph's worker threads.
use_cache_var: ContextVar[bool] = ContextVar("use_cache", default=True)


def enabled() -> bool:
    return get_settings().cache_enabled and use_cache_var.get()


def ttl_for(kind: str) -> int:
    s = get_settings()
    return {"embedding": s.cache_ttl_embedding_s, "search": s.cache_ttl_search_s,
            "answer": s.cache_ttl_answer_s, "health": 300}[kind]


def sha(parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def versioned_key(parts) -> str:
    """Key for search / answer entries: includes the current index version."""
    return f"{index_version()}:{sha(parts)}"


def _count(r, kind: str, hits: int, misses: int) -> None:
    if hits:
        r.incrby(f"{STATS_PREFIX}{kind}:hit", hits)
    if misses:
        r.incrby(f"{STATS_PREFIX}{kind}:miss", misses)


# --- bytes / json values ---------------------------------------------------------------------
def get_many(kind: str, keys: list[str]) -> list[bytes | None]:
    """Fetch several values (None for a miss). Returns all-None if Redis is unavailable."""
    empty: list[bytes | None] = [None] * len(keys)
    r = redis_client.get_redis()
    if r is None or not keys:
        return empty
    try:
        values = r.mget([PREFIX[kind] + k for k in keys])
        hits = sum(v is not None for v in values)
        _count(r, kind, hits, len(values) - hits)
        return values
    except redis_lib.RedisError as exc:
        redis_client.mark_down(exc)
        return empty


def set_many(kind: str, items: dict[str, bytes]) -> None:
    r = redis_client.get_redis()
    if r is None or not items:
        return
    try:
        pipe = r.pipeline()
        ttl = ttl_for(kind)
        for k, v in items.items():
            pipe.set(PREFIX[kind] + k, v, ex=ttl)
        pipe.execute()
    except redis_lib.RedisError as exc:
        redis_client.mark_down(exc)


def get_json(kind: str, key: str):
    raw = get_many(kind, [key])[0]
    return json.loads(raw) if raw is not None else None


def set_json(kind: str, key: str, value) -> None:
    set_many(kind, {key: json.dumps(value, default=str).encode()})


# --- vectors (float32 bytes) -------------------------------------------------------------------
def vec_to_bytes(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def bytes_to_vec(raw: bytes) -> list[float]:
    a = array("f")
    a.frombytes(raw)
    return a.tolist()


# --- index version -----------------------------------------------------------------------------
def index_version() -> int:
    r = redis_client.get_redis()
    if r is None:
        return 0
    try:
        v = r.get(INDEX_VERSION_KEY)
        return int(v) if v is not None else 1
    except redis_lib.RedisError as exc:
        redis_client.mark_down(exc)
        return 0


def bump_index_version() -> None:
    """Call whenever chunks were added, changed or removed."""
    r = redis_client.get_redis()
    if r is None:
        return
    try:
        if r.get(INDEX_VERSION_KEY) is None:
            r.set(INDEX_VERSION_KEY, 1)
        r.incr(INDEX_VERSION_KEY)
    except redis_lib.RedisError as exc:
        redis_client.mark_down(exc)


# --- admin ---------------------------------------------------------------------------------------
def ping() -> tuple[bool, str]:
    if not get_settings().cache_enabled:
        return False, "disabled (CACHE_ENABLED=false)"
    r = redis_client.get_redis(admin=True)
    try:
        r.ping()  # type: ignore[union-attr]
        return True, "ok"
    except redis_lib.RedisError as exc:
        return False, f"unreachable: {exc.__class__.__name__}"


def stats() -> dict:
    s = get_settings()
    out: dict = {"enabled": s.cache_enabled, "available": False, "kinds": {}}
    r = redis_client.get_redis(admin=True)
    if r is None:
        return out
    try:
        r.ping()
        out["available"] = True
        for kind in KINDS:
            hit = int(r.get(f"{STATS_PREFIX}{kind}:hit") or 0)
            miss = int(r.get(f"{STATS_PREFIX}{kind}:miss") or 0)
            keys = sum(1 for _ in r.scan_iter(match=PREFIX[kind] + "*", count=500))
            out["kinds"][kind] = {"hits": hit, "misses": miss, "keys": keys,
                                  "hit_rate": round(hit / (hit + miss), 3) if hit + miss else None,
                                  "ttl_s": ttl_for(kind)}
        v = r.get(INDEX_VERSION_KEY)
        out["index_version"] = int(v) if v is not None else 1
        try:  # memory figures are nice to have; never let them hide the rest of the stats
            info = r.info("memory")
            out["memory_used"] = info.get("used_memory_human")
            out["memory_max"] = info.get("maxmemory_human")
        except redis_lib.RedisError:
            out["memory_used"] = out["memory_max"] = None
    except redis_lib.RedisError as exc:
        out["error"] = f"{exc.__class__.__name__}: {exc}"
    return out


def clear(kind: str = "all") -> dict:
    kinds = KINDS if kind == "all" else (kind,)
    if any(k not in KINDS for k in kinds):
        raise ValueError(f"kind must be one of {KINDS} or 'all'")
    r = redis_client.get_redis(admin=True)
    if r is None:
        return {"cleared": {}, "available": False}
    cleared = {}
    try:
        for k in kinds:
            keys = list(r.scan_iter(match=PREFIX[k] + "*", count=500))
            if keys:
                r.delete(*keys)
            cleared[k] = len(keys)
    except redis_lib.RedisError as exc:
        return {"cleared": cleared, "available": False, "error": str(exc)}
    return {"cleared": cleared, "available": True}
