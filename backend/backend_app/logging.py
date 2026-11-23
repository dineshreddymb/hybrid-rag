"""Structured logging: JSON lines to stdout (podman logs) and to MongoDB `app_logs`.

Every log line carries `request_id` and `trace_id` (from contextvars), so a log row in the
UI can link straight to its Langfuse trace.

Writing to Mongo happens on a background thread with a bounded queue, so a slow or
missing database never blocks a request.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
from contextvars import ContextVar
from datetime import datetime, timezone

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

_log_queue: queue.Queue = queue.Queue(maxsize=10_000)
_writer_started = False
_STD_KEYS = {"event", "level", "timestamp", "service", "request_id", "trace_id", "logger"}


def _add_context(_, __, event_dict):
    event_dict.setdefault("request_id", request_id_var.get())
    event_dict.setdefault("trace_id", trace_id_var.get())
    return event_dict


def _make_service_adder(service: str):
    def _add_service(_, __, event_dict):
        event_dict.setdefault("service", service)
        return event_dict

    return _add_service


def _to_mongo_queue(_, __, event_dict):
    """Copy the event to the Mongo writer queue (drop it if the queue is full)."""
    try:
        doc = {
            "ts": datetime.now(timezone.utc),
            "level": str(event_dict.get("level", "info")).upper(),
            "service": event_dict.get("service"),
            "message": str(event_dict.get("event", "")),
            "request_id": event_dict.get("request_id"),
            "trace_id": event_dict.get("trace_id"),
            "extra": {k: _safe(v) for k, v in event_dict.items() if k not in _STD_KEYS},
        }
        _log_queue.put_nowait(doc)
    except queue.Full:
        pass
    return event_dict


def _safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:2000]


def _mongo_writer(mongo_uri: str, db_name: str):
    from pymongo import MongoClient

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    coll = client[db_name]["app_logs"]
    while True:
        batch = [_log_queue.get()]
        while len(batch) < 200:
            try:
                batch.append(_log_queue.get_nowait())
            except queue.Empty:
                break
        try:
            coll.insert_many(batch, ordered=False)
        except Exception:  # noqa: BLE001 - logging must never crash the app
            pass


def setup_logging(service: str, level: str = "INFO", mongo_uri: str | None = None,
                  db_name: str = "hybrid_rag") -> None:
    global _writer_started
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _make_service_adder(service),
        _add_context,
    ]
    if mongo_uri:
        processors.append(_to_mongo_queue)
        if not _writer_started:
            threading.Thread(target=_mongo_writer, args=(mongo_uri, db_name),
                             daemon=True, name="mongo-log-writer").start()
            _writer_started = True
    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    # Keep noisy libraries quiet.
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout)


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
