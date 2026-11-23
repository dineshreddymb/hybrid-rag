"""Small Redis-backed HTTP rate limiter.

The backend has one public entry point, so an application-level limiter protects both the
Dash UI and MCP HTTP server without adding another container.  It uses a fixed one-minute
window, which is intentional: it is observable, inexpensive and works across API replicas.
If Redis is unavailable, requests are allowed and a warning is logged (the service remains
usable rather than becoming unavailable because an optional dependency failed).
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import redis

from backend_app.cache import redis_client
from backend_app.config import get_settings
from backend_app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class LimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_s: int


def _identity(client_host: str | None, api_key: str | None) -> str:
    """Do not place an API key or raw address in Redis keys."""
    raw = api_key or client_host or "unknown"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def check(path: str, *, client_host: str | None, api_key: str | None = None,
          now: float | None = None) -> LimitResult:
    s = get_settings()
    limit = s.rate_limit_ask_per_minute if path == "/ask" else s.rate_limit_requests_per_minute
    if not s.rate_limit_enabled:
        return LimitResult(True, limit, limit, 0)

    ts = time.time() if now is None else now
    bucket = int(ts // 60)
    retry_after_s = max(1, 60 - int(ts % 60))
    key = f"ratelimit:{bucket}:{'ask' if path == '/ask' else 'api'}:{_identity(client_host, api_key)}"
    try:
        # Reuse the existing fail-open Redis connection. The cache setting does not gate
        # creation of a client here: rate limiting remains available when response caching is off.
        client = redis_client.get_redis(admin=True, require_cache_enabled=False)
        if client is None:
            return LimitResult(True, limit, limit, 0)
        count = int(client.incr(key))
        if count == 1:
            client.expire(key, 61)
        return LimitResult(count <= limit, limit, max(0, limit - count), retry_after_s)
    except redis.RedisError as exc:
        log.warning("rate_limit_unavailable", error=exc.__class__.__name__)
        return LimitResult(True, limit, limit, 0)
