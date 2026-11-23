"""Scheduled Qdrant snapshot retention for the backend container.

Snapshots are written by Qdrant to its mounted /qdrant/snapshots directory, which compose maps
to data/qdrant_snapshots on the host. This is deliberately a daemon thread: a failed backup
must never prevent the API from starting or answering questions.
"""
from __future__ import annotations

import threading
import re
from datetime import datetime, timezone

from backend_app.config import get_settings
from backend_app.logging import get_logger
from backend_app.vectorstore import qdrant_store

log = get_logger(__name__)
_stop = threading.Event()
_thread: threading.Thread | None = None
_state: dict[str, str | int | None] = {"last_status": "not_run", "last_snapshot": None,
                                        "last_error": None, "deleted": 0}


def snapshot_once() -> dict:
    """Create a snapshot and retain the newest configured number of snapshots."""
    global _state
    try:
        snap = qdrant_store.create_snapshot()
        snapshots = qdrant_store.list_snapshots()
        # Qdrant's generated names end in YYYY-MM-DD-HH-MM-SS.  Some server versions return
        # no creation_time, so use that suffix; for nonstandard names preserve Qdrant's list
        # order rather than deleting a random backup.
        def newest_first(item: dict) -> str:
            match = re.search(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})", item["name"])
            return match.group(1) if match else ""
        old = sorted(snapshots, key=newest_first, reverse=True)[get_settings().qdrant_snapshot_keep:]
        for item in old:
            qdrant_store.delete_snapshot(item["name"])
        _state = {"last_status": "ok", "last_snapshot": snap["name"], "last_error": None,
                  "deleted": len(old), "last_finished_at": datetime.now(timezone.utc).isoformat()}
        log.info("qdrant_snapshot_created", snapshot=snap["name"], deleted=len(old))
    except Exception as exc:  # noqa: BLE001
        _state = {"last_status": "failed", "last_snapshot": None,
                  "last_error": f"{exc.__class__.__name__}: {exc}", "deleted": 0,
                  "last_finished_at": datetime.now(timezone.utc).isoformat()}
        log.error("qdrant_snapshot_failed", error=str(exc))
    return dict(_state)


def status() -> dict:
    s = get_settings()
    return {**_state, "enabled": s.qdrant_snapshot_schedule_enabled,
            "interval_hours": s.qdrant_snapshot_interval_hours,
            "keep": s.qdrant_snapshot_keep}


def _run() -> None:
    interval_s = get_settings().qdrant_snapshot_interval_hours * 3600
    while not _stop.wait(interval_s):
        snapshot_once()


def start() -> None:
    global _thread
    if not get_settings().qdrant_snapshot_schedule_enabled or _thread is not None:
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, daemon=True, name="qdrant-snapshot-scheduler")
    _thread.start()
    log.info("qdrant_snapshot_scheduler_started", interval_hours=get_settings().qdrant_snapshot_interval_hours)


def stop() -> None:
    global _thread
    _stop.set()
    _thread = None
