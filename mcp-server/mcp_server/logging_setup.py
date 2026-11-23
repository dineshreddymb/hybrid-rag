"""JSON-lines logging for the MCP server, plus best-effort shipping to the backend's `POST /logs/batch`.

* stdio transport: logs go to **stderr** (stdout carries the MCP protocol - never write there).
* http transport: logs go to stdout (what `podman logs` shows).
* Each record is also queued (bounded, drop-when-full) and POSTed in batches by a daemon thread to
  {BACKEND_URL or API_URL}/logs/batch as {"service": "mcp", "entries": [{ts, level, message,
  request_id, trace_id, extra}]}. The shipper is silent: it never prints and never raises.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

import httpx

SERVICE = "mcp"
DEFAULT_BACKEND_URL = "http://backend:8000"
BATCH_MAX = 100
FLUSH_SECONDS = 2.0
_SHIPPER_NAME = "log-shipper"
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message", "asctime", "color_message"}  # color_message: uvicorn's ANSI duplicate
_log_queue: queue.Queue = queue.Queue(maxsize=10_000)
_writer_started = False
_writer_lock = threading.Lock()


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value][:50]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in list(value.items())[:50]}
    return str(value)[:2000]


def _extra(record: logging.LogRecord) -> dict[str, Any]:
    out = {k: _safe(v) for k, v in record.__dict__.items()
           if k not in _STD_ATTRS and k not in ("request_id", "trace_id")}
    if record.name and not record.name.startswith("mcp_server"):
        out["logger"] = record.name
    if record.exc_info:
        out["exc"] = logging.Formatter().formatException(record.exc_info)[-2000:]
    return out


def _fields(record: logging.LogRecord) -> tuple[str | None, str | None]:
    rid = getattr(record, "request_id", None) or request_id_var.get()
    return rid, getattr(record, "trace_id", None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rid, tid = _fields(record)
        line = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE,
            "message": record.getMessage(),
            "request_id": rid,
            "trace_id": tid,
            **_extra(record),
        }
        return json.dumps(line, default=str, ensure_ascii=False)


class HttpQueueHandler(logging.Handler):
    """Puts an entry on the queue; never blocks, never raises."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if threading.current_thread().name == _SHIPPER_NAME or record.name.startswith(("httpx", "httpcore")):
                return  # the shipper's own traffic must not be shipped
            rid, tid = _fields(record)
            _log_queue.put_nowait({
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                "request_id": rid,
                "trace_id": tid,
                "extra": _extra(record),
            })
        except Exception:  # noqa: BLE001 - queue.Full or anything else: logging must never crash the server
            pass


def _make_client() -> httpx.Client:
    """Bare client (no logging hooks); short timeouts so a dead backend costs little."""
    return httpx.Client(timeout=httpx.Timeout(3.0, connect=2.0))


def _ship_batch(client: httpx.Client, batch: list[dict[str, Any]]) -> None:
    base = (os.getenv("BACKEND_URL") or os.getenv("API_URL") or DEFAULT_BACKEND_URL).rstrip("/")
    headers = {"X-API-Key": os.environ["API_KEY"]} if os.getenv("API_KEY") else {}
    client.post(f"{base}/logs/batch", json={"service": SERVICE, "entries": batch}, headers=headers)


def _shipper() -> None:
    """Daemon loop: send up to 100 entries per POST, at most ~2 s after the first one arrives."""
    try:
        client = _make_client()
    except Exception:  # noqa: BLE001
        return
    while True:
        try:
            batch = [_log_queue.get()]
            deadline = time.monotonic() + FLUSH_SECONDS
            while len(batch) < BATCH_MAX:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(_log_queue.get(timeout=remaining))
                except queue.Empty:
                    break
            _ship_batch(client, batch)
        except Exception:  # noqa: BLE001 - backend down: drop the batch, keep running
            pass


def setup_logging(transport: str = "stdio", level: str | None = None,
                  ship_logs: bool = True) -> None:
    """Replace root handlers with one JSON handler (+ HTTP shipper to the backend if enabled)."""
    global _writer_started
    level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    stream = sys.stderr if transport == "stdio" else sys.stdout
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handlers: list[logging.Handler] = [handler]

    if ship_logs:
        handlers.append(HttpQueueHandler())
        with _writer_lock:
            if not _writer_started:
                threading.Thread(target=_shipper, daemon=True, name=_SHIPPER_NAME).start()
                _writer_started = True

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(logging.WARNING)                 # libraries: warnings only
    logging.getLogger("mcp_server").setLevel(level)  # our own logs at LOG_LEVEL
    logging.getLogger("uvicorn").setLevel(level)     # "Uvicorn running on ..." etc.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str = "mcp_server") -> logging.Logger:
    return logging.getLogger(name)
