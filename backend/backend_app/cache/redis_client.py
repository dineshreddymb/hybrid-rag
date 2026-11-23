"""Redis connection for the cache. Fail-open: if Redis is off, down or slow, callers get None
and the backend works without the cache (same rule as Langfuse tracing).

- Normal calls use a 50 ms socket timeout.
- After a failure we skip Redis for 10 s, so a dead Redis costs one timeout, not one per call.
- Admin calls (stats, clear) use a separate client with a longer timeout.
"""
from __future__ import annotations

import threading
import time

import redis

from backend_app.config import get_settings
from backend_app.logging import get_logger

log = get_logger(__name__)

COMMAND_TIMEOUT_S = 0.05
ADMIN_TIMEOUT_S = 3.0
SKIP_AFTER_FAILURE_S = 10.0

_clients: dict[str, redis.Redis] = {}
_lock = threading.Lock()
_down_until = 0.0


def _make(timeout: float) -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url_resolved, socket_timeout=timeout,
                                socket_connect_timeout=max(timeout, 0.25),
                                decode_responses=False)


def get_redis(admin: bool = False, require_cache_enabled: bool = True) -> redis.Redis | None:
    """A Redis client, or None when the cache is disabled or Redis was just seen failing."""
    if require_cache_enabled and not get_settings().cache_enabled:
        return None
    if not admin and time.monotonic() < _down_until:
        return None
    kind = "admin" if admin else "fast"
    client = _clients.get(kind)
    if client is None:
        with _lock:
            client = _clients.get(kind)
            if client is None:
                client = _clients[kind] = _make(ADMIN_TIMEOUT_S if admin else COMMAND_TIMEOUT_S)
    return client


def mark_down(exc: Exception) -> None:
    global _down_until
    _down_until = time.monotonic() + SKIP_AFTER_FAILURE_S
    log.warning("redis_unavailable", error=f"{exc.__class__.__name__}: {exc}",
                skipping_for_s=SKIP_AFTER_FAILURE_S)


def reset(client: redis.Redis | None = None) -> None:
    """Tests: install a fake client (or None to forget all clients)."""
    global _down_until
    _clients.clear()
    _down_until = 0.0
    if client is not None:
        _clients["fast"] = _clients["admin"] = client
