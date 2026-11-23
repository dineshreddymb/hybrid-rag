"""Remote Cloud Storage Sync with Parallel Folder Traversal, Deep Ingestion,
and Supabase Bucket Auto-Discovery.

Features:
- Full Bucket Auto-Discovery: Lists all objects in Supabase bucket using Anon Key.
- Dual-Mode Ingestion:
  - If object is .zip: In-memory parallel folder decomposition across worker threads.
  - If object is .md/.txt: Direct streaming to ingestion pipeline.
- Lightweight ETag / metadata checking before downloading.
- Rich telemetry for dashboard reporting (folders, paths, time, space).
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import time
import urllib.error
import urllib.parse
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

# Individual object ETags for bucket auto-discovery
_file_etags: dict[str, str] = {}


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
    """Streams the remote ZIP or text file directly into memory."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "HybridRAG-CloudSync/1.0", "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def list_supabase_bucket_objects(
    supabase_url: str,
    bucket: str,
    anon_key: str,
    prefix: str = "",
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Calls Supabase Storage REST API to list all objects in the bucket."""
    api_url = f"{supabase_url.rstrip('/')}/storage/v1/object/list/{bucket}"
    payload = json.dumps({"prefix": prefix, "limit": 100, "sortBy": {"column": "name", "order": "asc"}}).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
            "User-Agent": "HybridRAG-CloudSync/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def sync_supabase_bucket(
    supabase_url: str,
    bucket: str,
    anon_key: str,
    force: bool = False,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Auto-discovers and syncs all objects (.zip, .md, .txt) in the Supabase bucket."""
    started_at = datetime.now(timezone.utc).isoformat()
    t_start = time.perf_counter()
    _sync_state["last_checked_at"] = started_at

    objects = []
    try:
        objects = list_supabase_bucket_objects(supabase_url, bucket, anon_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("supabase_bucket_list_failed", error=str(exc))

    # Automatic fallback for RLS blocks or empty listing:
    # If bucket listing returns empty or errors (e.g. Supabase Storage RLS has no SELECT policy),
    # probe for a direct public docs.zip or configured docs_remote_zip_url.
    if not objects:
        fallback_candidates = []
        s = get_settings()
        if s.docs_remote_zip_url:
            candidate_name = s.docs_remote_zip_url.rstrip("/").split("/")[-1] or "docs.zip"
            fallback_candidates.append((candidate_name, s.docs_remote_zip_url))
        default_public_zip = f"{supabase_url.rstrip('/')}/storage/v1/object/public/{bucket}/docs.zip"
        if not any(c[1] == default_public_zip for c in fallback_candidates):
            fallback_candidates.append(("docs.zip", default_public_zip))

        for cand_name, cand_url in fallback_candidates:
            head = check_remote_head(cand_url)
            if head.get("ok"):
                log.info("supabase_bucket_rls_fallback_detected", name=cand_name, url=cand_url)
                objects = [{"name": cand_name, "url": cand_url, "metadata": {"eTag": head.get("etag", "")}}]
                break

    all_items: list[dict] = []
    folder_metrics: list[dict] = []
    discovered_folders: set[str] = set()
    synced_objects: list[str] = []

    for obj in objects:
        name = obj.get("name") or ""
        metadata = obj.get("metadata") or {}
        etag = str(metadata.get("eTag") or obj.get("updated_at") or name).strip("\"'")
        last_etag = _file_etags.get(name)

        if not force and last_etag and last_etag == etag:
            continue

        public_url = obj.get("url") or f"{supabase_url.rstrip('/')}/storage/v1/object/public/{bucket}/{name}"
        try:
            raw_bytes = download_remote_zip(public_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("bucket_object_download_failed", name=name, error=str(exc))
            continue

        is_zip = name.lower().endswith(".zip") or raw_bytes.startswith(b"PK\x03\x04")
        if is_zip:
            folder_groups = group_zip_by_folders(raw_bytes)
            num_workers = max(1, min(len(folder_groups), max_workers))
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = {
                    executor.submit(_crawl_folder_worker, f, members, raw_bytes, force): f
                    for f, members in folder_groups.items()
                }
                for future in concurrent.futures.as_completed(futures):
                    folder_items, folder_stats = future.result()
                    all_items.extend(folder_items)
                    folder_metrics.append(folder_stats)
                    discovered_folders.add(folder_stats["folder"])
        elif any(name.lower().endswith(s) for s in ALLOWED_SUFFIXES):
            folder_name = name.split("/")[0] if "/" in name else "root"
            discovered_folders.add(folder_name)
            text = raw_bytes.decode("utf-8", errors="replace")
            size_b = len(raw_bytes)
            all_items.append({
                "source": name,
                "content": text,
                "origin": "cloud_sync",
                "folder": folder_name,
                "size": size_b,
                "force": force,
            })
            folder_metrics.append({
                "folder": folder_name,
                "files_count": 1,
                "total_bytes": size_b,
                "duration_ms": 1.0,
                "files": [name],
            })

        _file_etags[name] = etag
        synced_objects.append(name)

    _sync_state["cached_folders"] = sorted(discovered_folders)

    if not all_items:
        _sync_state["last_status"] = "unchanged" if objects else "empty"
        return {
            "status": "unchanged" if objects else "empty",
            "message": "All bucket objects are unchanged." if objects else "Bucket is empty.",
            "bucket": bucket,
            "total_objects": len(objects),
            "synced_objects": [],
            "folders": sorted(discovered_folders),
            "folder_metrics": _sync_state.get("folder_metrics", []),
        }

    job_result = pipeline.ingest_items(all_items, trigger="cloud_sync", prune_missing=False)
    total_duration_s = round(time.perf_counter() - t_start, 2)

    job_map = {r.get("source"): r for r in (job_result.get("results") or [])}
    file_metrics = []
    for item in all_items:
        src = item["source"]
        r = job_map.get(src) or {}
        raw_status = r.get("status", "ingested")
        is_dup = bool(r.get("is_duplicate") or raw_status == "unchanged")
        status_label = "Already Existing" if is_dup else raw_status
        file_metrics.append({
            "source": src,
            "folder": item.get("folder", "root"),
            "size_bytes": item.get("size", 0),
            "status": status_label,
            "is_duplicate": is_dup,
            "doc_id": r.get("doc_id"),
            "chunks_new": r.get("chunks_new", 0),
            "chunks_reused": r.get("chunks_reused", 0),
        })

    _sync_state.update({
        "last_etag": ",".join(f"{k}:{v[:8]}" for k, v in list(_file_etags.items())[:5]),
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "last_status": "synced",
        "last_error": None,
        "last_job_id": job_result.get("job_id"),
        "folder_metrics": folder_metrics,
        "file_metrics": file_metrics,
    })

    return {
        "status": "synced",
        "bucket": bucket,
        "total_files": len(all_items),
        "total_folders": len(discovered_folders),
        "synced_objects": synced_objects,
        "duration_s": total_duration_s,
        "job_id": job_result.get("job_id"),
        "folder_metrics": folder_metrics,
        "file_metrics": file_metrics,
        "job": job_result,
    }


def sync_remote_zip(
    url: str | None = None,
    force: bool = False,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Main orchestration:

    - If Supabase bucket & anon key configured and no explicit URL given: runs Bucket Auto-Discovery.
    - Otherwise: runs single URL sync (.zip or direct text).
    """
    s = get_settings()

    # Mode A: Full Supabase Bucket Auto-Discovery
    if s.supabase_anon_key and s.supabase_url and s.supabase_bucket and not url:
        res = sync_supabase_bucket(
            supabase_url=s.supabase_url,
            bucket=s.supabase_bucket,
            anon_key=s.supabase_anon_key,
            force=force,
            max_workers=max_workers,
        )
        if res.get("status") in ("synced", "unchanged"):
            return res
        # If bucket auto-discovery returned empty or error, but docs_remote_zip_url is explicitly configured,
        # fallback to single URL sync (Mode B)
        if (s.docs_remote_zip_url or "").strip():
            log.info("supabase_bucket_empty_falling_back_to_url", url=s.docs_remote_zip_url)
        else:
            return res

    # Mode B: Single URL Sync (Fallback)
    target_url = (url or s.docs_remote_zip_url or "").strip()
    if not target_url:
        return {"status": "error", "message": "No remote ZIP URL or Supabase credentials configured."}

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
        raw_bytes = download_remote_zip(target_url)
    except Exception as exc:  # noqa: BLE001
        _sync_state["last_status"] = "error"
        _sync_state["last_error"] = str(exc)
        return {"status": "error", "message": f"Download failed: {exc}"}

    all_items: list[dict] = []
    folder_metrics: list[dict] = []
    is_zip = target_url.lower().split("?")[0].endswith(".zip") or raw_bytes.startswith(b"PK\x03\x04")

    if is_zip:
        folder_groups = group_zip_by_folders(raw_bytes)
        folder_names = sorted(folder_groups.keys())
        _sync_state["cached_folders"] = folder_names

        if not folder_groups:
            _sync_state["last_status"] = "empty"
            return {"status": "empty", "message": "No .md or .txt documents found in the zip archive."}

        num_workers = max(1, min(len(folder_groups), max_workers))
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(_crawl_folder_worker, f, members, raw_bytes, force): f
                for f, members in folder_groups.items()
            }
            for future in concurrent.futures.as_completed(futures):
                folder_items, folder_stats = future.result()
                all_items.extend(folder_items)
                folder_metrics.append(folder_stats)
    else:
        parsed = urllib.parse.urlparse(target_url)
        path_clean = parsed.path.replace("\\", "/").strip("/")

        if "/object/public/" in parsed.path:
            rel_path = parsed.path.split("/object/public/", 1)[1]
            parts = rel_path.split("/", 1)
            file_path = parts[1] if len(parts) > 1 and parts[0] == "docs" else rel_path
        else:
            file_path = path_clean.split("/")[-1] or "document.md"

        folder_name = file_path.split("/")[0] if "/" in file_path else "root"
        _sync_state["cached_folders"] = [folder_name]

        text = raw_bytes.decode("utf-8", errors="replace")
        size_b = len(raw_bytes)
        all_items = [{
            "source": file_path,
            "content": text,
            "origin": "cloud_sync",
            "folder": folder_name,
            "size": size_b,
            "force": force,
        }]
        folder_metrics = [{
            "folder": folder_name,
            "files_count": 1,
            "total_bytes": size_b,
            "duration_ms": 1.0,
            "files": [file_path],
        }]

    job_result = pipeline.ingest_items(all_items, trigger="cloud_sync", prune_missing=False)
    total_duration_s = round(time.perf_counter() - t_start, 2)

    job_map = {r.get("source"): r for r in (job_result.get("results") or [])}
    file_metrics = []
    for item in all_items:
        src = item["source"]
        r = job_map.get(src) or {}
        raw_status = r.get("status", "ingested")
        is_dup = bool(r.get("is_duplicate") or raw_status == "unchanged")
        status_label = "Already Existing" if is_dup else raw_status
        file_metrics.append({
            "source": src,
            "folder": item.get("folder", "root"),
            "size_bytes": item.get("size", 0),
            "status": status_label,
            "is_duplicate": is_dup,
            "doc_id": r.get("doc_id"),
            "chunks_new": r.get("chunks_new", 0),
            "chunks_reused": r.get("chunks_reused", 0),
        })

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
        "total_folders": len(folder_metrics),
        "duration_s": total_duration_s,
        "job_id": job_result.get("job_id"),
        "folder_metrics": folder_metrics,
        "file_metrics": file_metrics,
        "job": job_result,
    }


def get_sync_status() -> dict[str, Any]:
    """Returns current cloud sync status and telemetry for dashboard display."""
    s = get_settings()
    mode = "bucket" if s.supabase_anon_key else "url"
    target = f"{s.supabase_url}/storage/v1/object/public/{s.supabase_bucket}" if mode == "bucket" else s.docs_remote_zip_url
    return {
        "enabled": s.docs_sync_enabled,
        "mode": mode,
        "url": target,
        "bucket": s.supabase_bucket,
        "interval_s": s.docs_sync_interval_seconds,
        **_sync_state,
    }
