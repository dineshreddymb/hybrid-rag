"""Remote Cloud Storage Sync with Parallel Folder Traversal and Ingestion.

Features:
- Lightweight HTTP HEAD polling to inspect ETag / Last-Modified before downloading.
- In-memory parallel folder decomposition using ThreadPoolExecutor.
- Deep recursive traversal preserving full relative paths for citation accuracy.
- Folder-level caching and rich space/time telemetry for dashboard reporting.
"""
from __future__ import annotations

import concurrent.futures
import io
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Any

from backend_app.config import get_settings
from backend_app.ingestion import pipeline
from backend_app.logging import get_logger

log = get_logger(__name__)

ALLOWED_SUFFIXES = (".md", ".txt")

# In-memory telemetry cache for cloud sync status and folder breakdown
_sync_state: dict[str, Any] = {
    "last_etag": None,
    "last_checked_at": None,
    "last_synced_at": None,
    "last_status": "idle",
    "last_error": None,
    "last_job_id": None,
    "cached_folders": [],
    "folder_metrics": [],
    "file_metrics": [],
}


def check_remote_head(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """Sends a fast HTTP HEAD request to check ETag, Last-Modified, and Content-Length."""
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "HybridRAG-CloudSync/1.0", "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = resp.headers
            etag = (headers.get("ETag") or "").strip("\"'")
            last_mod = headers.get("Last-Modified") or ""
            content_length = int(headers.get("Content-Length") or 0)
            return {
                "ok": True,
                "status_code": resp.status,
                "etag": etag,
                "last_modified": last_mod,
                "content_length": content_length,
            }
    except Exception as exc:  # noqa: BLE001
        log.warning("remote_head_check_failed", url=url, error=str(exc))
        return {
            "ok": False,
            "status_code": getattr(exc, "code", 500) if hasattr(exc, "code") else 500,
            "etag": "",
            "last_modified": "",
            "content_length": 0,
            "error": str(exc),
        }


def download_remote_zip(url: str, timeout: float = 45.0) -> bytes:
    """Streams the remote ZIP archive directly into memory."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "HybridRAG-CloudSync/1.0", "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def group_zip_by_folders(zip_bytes: bytes) -> dict[str, list[zipfile.ZipInfo]]:
    """Inspects the ZIP in-memory and groups eligible .md/.txt files by their folder branch."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        groups: dict[str, list[zipfile.ZipInfo]] = {}
        for member in z.infolist():
            if member.is_dir():
                continue
            name_lower = member.filename.lower()
            if not any(name_lower.endswith(s) for s in ALLOWED_SUFFIXES):
                continue

            # Determine top-level or immediate folder path
            parts = member.filename.replace("\\", "/").split("/")
            folder = parts[0] if len(parts) > 1 else "root"
            groups.setdefault(folder, []).append(member)
        return groups


def _crawl_folder_worker(
    folder: str,
    members: list[zipfile.ZipInfo],
    zip_bytes: bytes,
    force: bool,
) -> tuple[list[dict], dict]:
    """Parallel worker: extracts all files in an assigned folder and computes time/space metrics."""
    t0 = time.perf_counter()
    items: list[dict] = []
    folder_bytes = 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for m in members:
            raw = z.read(m)
            text = raw.decode("utf-8", errors="replace")
            size_b = len(raw)
            folder_bytes += size_b
            items.append({
                "source": m.filename.replace("\\", "/"),
                "content": text,
                "origin": "cloud_sync",
                "folder": folder,
                "size": size_b,
                "force": force,
            })

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    stats = {
        "folder": folder,
        "files_count": len(items),
        "total_bytes": folder_bytes,
        "duration_ms": elapsed_ms,
        "files": [i["source"] for i in items],
    }
    log.info("folder_traversed_parallel", folder=folder, files=len(items), duration_ms=elapsed_ms)
    return items, stats


def sync_remote_zip(
    url: str | None = None,
    force: bool = False,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Main orchestration:

    1. Checks HEAD / ETag.
    2. If unchanged and not force, skips download.
    3. Downloads ZIP into memory.
    4. Groups into folder branches and spawns parallel workers.
    5. Feeds discovered items with full relative paths into the ingestion pipeline.
    6. Stores telemetry for dashboard reporting.
    """
    s = get_settings()
    target_url = (url or s.docs_remote_zip_url or "").strip()
    if not target_url:
        return {"status": "error", "message": "No remote ZIP URL configured."}

    started_at = datetime.now(timezone.utc).isoformat()
    t_start = time.perf_counter()

    head = check_remote_head(target_url)
    _sync_state["last_checked_at"] = started_at

    if not head.get("ok"):
        _sync_state["last_status"] = "error"
        _sync_state["last_error"] = head.get("error")
        return {
            "status": "error",
            "message": f"Could not reach remote storage: {head.get('error')}",
            "head": head,
        }

    current_etag = head.get("etag") or ""
    last_etag = _sync_state.get("last_etag")

    # If ETag has not changed and not forced, exit early with zero download
    if not force and current_etag and last_etag == current_etag:
        _sync_state["last_status"] = "unchanged"
        return {
            "status": "unchanged",
            "message": "Remote zip unchanged (ETag matched). Ingestion skipped.",
            "etag": current_etag,
            "url": target_url,
            "folders": _sync_state.get("cached_folders", []),
            "folder_metrics": _sync_state.get("folder_metrics", []),
        }

    log.info("remote_sync_downloading", url=target_url, etag=current_etag, force=force)
    try:
        zip_bytes = download_remote_zip(target_url)
    except Exception as exc:  # noqa: BLE001
        _sync_state["last_status"] = "error"
        _sync_state["last_error"] = str(exc)
        return {"status": "error", "message": f"Download failed: {exc}"}

    # Group files into folder branches
    folder_groups = group_zip_by_folders(zip_bytes)
    folder_names = sorted(folder_groups.keys())
    _sync_state["cached_folders"] = folder_names

    if not folder_groups:
        _sync_state["last_status"] = "empty"
        return {"status": "empty", "message": "No .md or .txt documents found in the zip archive."}

    # Dispatch parallel workers across folder branches
    all_items: list[dict] = []
    folder_metrics: list[dict] = []
    num_workers = max(1, min(len(folder_groups), max_workers))

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(_crawl_folder_worker, f, members, zip_bytes, force): f
            for f, members in folder_groups.items()
        }
        for future in concurrent.futures.as_completed(futures):
            folder_items, folder_stats = future.result()
            all_items.extend(folder_items)
            folder_metrics.append(folder_stats)

    # Ingest discovered items preserving full relative paths
    job_result = pipeline.ingest_items(all_items, trigger="cloud_sync", prune_missing=False)
    total_duration_s = round(time.perf_counter() - t_start, 2)

    # Prepare detailed file-level metrics (space, time, chunks)
    job_map = {r.get("source"): r for r in (job_result.get("results") or [])}
    file_metrics = []
    for item in all_items:
        src = item["source"]
        r = job_map.get(src) or {}
        file_metrics.append({
            "source": src,
            "folder": item.get("folder", "root"),
            "size_bytes": item.get("size", 0),
            "status": r.get("status", "ingested"),
            "chunks_new": r.get("chunks_new", 0),
            "chunks_reused": r.get("chunks_reused", 0),
        })

    # Update state cache
    _sync_state.update({
        "last_etag": current_etag,
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "last_status": "synced",
        "last_error": None,
        "last_job_id": job_result.get("job_id"),
        "folder_metrics": folder_metrics,
        "file_metrics": file_metrics,
    })

    return {
        "status": "synced",
        "etag": current_etag,
        "url": target_url,
        "total_files": len(all_items),
        "total_folders": len(folder_groups),
        "duration_s": total_duration_s,
        "job_id": job_result.get("job_id"),
        "folder_metrics": folder_metrics,
        "file_metrics": file_metrics,
        "job": job_result,
    }


def get_sync_status() -> dict[str, Any]:
    """Returns current cloud sync status and telemetry for dashboard display."""
    s = get_settings()
    return {
        "enabled": s.docs_sync_enabled,
        "url": s.docs_remote_zip_url,
        "interval_s": s.docs_sync_interval_seconds,
        **_sync_state,
    }
