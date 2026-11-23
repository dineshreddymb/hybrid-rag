"""Logging for the UI: JSON lines to stdout, plus best-effort shipping to the backend.

Rows are POSTed in batches to `{BACKEND_URL}/logs/batch` (the backend stores them in `app_logs`)
by a background thread fed by a bounded queue, so a slow or missing backend never blocks a page
or crashes the app: when the queue is full, log rows are dropped.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone

SERVICE = "ui"

_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}
_log_queue: queue.Queue = queue.Queue(maxsize=5_000)
_lock = threading.Lock()
_configured = False

logger = logging.getLogger("ui")


def _extra(record: logging.LogRecord) -> dict:
    out = {}
    for k, v in vars(record).items():
        if k in _STD_ATTRS or k in ("request_id", "trace_id"):
            continue
        out[k] = v if isinstance(v, (str, int, float, bool)) or v is None else str(v)[:2000]
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "trace_id": getattr(record, "trace_id", None),
            **_extra(record),
        }
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


class LogShipper:
    """Background thread that POSTs queued log rows to `{BACKEND_URL}/logs/batch`.

    Uses its own bare httpx client (not ApiClient) and never logs, so shipping logs cannot
    create more logs. `put` never blocks; when the queue is full the row is dropped.
    """

    MAX_BATCH = 100
    FLUSH_S = 2.0

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 transport=None, maxsize: int = 5_000, start: bool = True):
        import httpx

        self.base_url = (base_url or os.getenv("BACKEND_URL") or os.getenv("API_URL")
                         or "http://backend:8000").rstrip("/")
        key = os.getenv("API_KEY", "") if api_key is None else api_key
        headers = {"X-API-Key": key} if key else {}
        self._http = httpx.Client(base_url=self.base_url, headers=headers, transport=transport,
                                  timeout=httpx.Timeout(5.0, connect=2.0))
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, daemon=True, name="log-shipper")
            self._thread.start()

    def put(self, entry: dict) -> None:
        try:
            self.queue.put_nowait(entry)
        except Exception:  # noqa: BLE001 - queue.Full or anything else: drop
            pass

    def drain(self) -> list[dict]:
        batch: list[dict] = []
        while len(batch) < self.MAX_BATCH:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def flush_once(self) -> bool:
        """Send one batch (up to MAX_BATCH rows). Returns False if the send failed."""
        batch = self.drain()
        if not batch:
            return True
        try:
            self._http.post("/logs/batch", json={"service": SERVICE, "entries": batch})
            return True
        except Exception:  # noqa: BLE001 - drop the batch; logging must never crash the app
            return False

    def _run(self) -> None:
        while True:
            try:
                deadline = time.monotonic() + self.FLUSH_S
                while self.queue.qsize() < self.MAX_BATCH and time.monotonic() < deadline:
                    time.sleep(0.1)
                if not self.flush_once():
                    time.sleep(2)
            except Exception:  # noqa: BLE001
                time.sleep(2)


class HttpQueueHandler(logging.Handler):
    """Puts log rows on the shipper's queue; never blocks and never raises."""

    def __init__(self, shipper: LogShipper):
        super().__init__()
        self.shipper = shipper

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.shipper.put({
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                "request_id": getattr(record, "request_id", None),
                "trace_id": getattr(record, "trace_id", None),
                "extra": _extra(record),
            })
        except Exception:  # noqa: BLE001 - logging must never crash the app
            pass


def setup_logging(level: str | None = None) -> logging.Logger:
    """Configure the `ui` logger once (safe to call many times)."""
    global _configured
    with _lock:
        if _configured:
            return logger
        logger.setLevel((level or os.getenv("LOG_LEVEL", "INFO")).upper())
        logger.propagate = False
        stdout = logging.StreamHandler(sys.stdout)
        stdout.setFormatter(JsonFormatter())
        logger.addHandler(stdout)
        if os.getenv("LOG_SHIPPING", "1").lower() not in ("0", "false", "no", "off"):
            logger.addHandler(HttpQueueHandler(LogShipper()))
        _configured = True
    return logger
