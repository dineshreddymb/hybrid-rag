"""FastAPI routes. One API serves the Dash UI, the MCP server and the eval job.

Run locally:  uvicorn backend_app.main:app --reload --port 8000   (from backend/)
API docs:     http://localhost:8000/docs
"""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend_app.cache import store as cache
from backend_app import backups
from backend_app.config import get_settings
from backend_app.db import mongo
from backend_app.db import repositories as repo
from backend_app.embeddings import get_embedder
from backend_app.embeddings.providers import check_embeddings
from backend_app.graph.service import ask as rag_ask
from backend_app.ingestion import pipeline
from backend_app.llm.client import check_llm
from backend_app.logging import get_logger, request_id_var, setup_logging
from backend_app.retrieval import bm25
from backend_app.retrieval.service import hybrid_search
from backend_app.security.rate_limit import check as check_rate_limit
from backend_app.tracing import check_langfuse, flush, score, trace_request, trace_url
from backend_app.vectorstore import qdrant_store

log = get_logger("api")


def _startup_tasks() -> None:
    """Connect to stores and (optionally) ingest data/docs. Runs in the background so the
    API starts even if a dependency is still booting."""
    s = get_settings()
    for attempt in range(30):
        try:
            mongo.ensure_indexes()
            qdrant_store.ensure_collection()
            bm25.rebuild()
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("startup_waiting_for_dependencies", attempt=attempt, error=str(exc))
            time.sleep(2)
    if s.auto_ingest_on_startup:
        try:
            pipeline.scan_docs_dir(trigger="startup")
        except Exception as exc:  # noqa: BLE001
            log.error("startup_ingest_failed", error=str(exc))
    if s.docs_sync_enabled and s.docs_remote_zip_url:
        try:
            from backend_app.ingestion import remote_sync
            remote_sync.sync_remote_zip(force=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("startup_remote_sync_failed", error=str(exc))


def _remote_sync_loop(stop_event: threading.Event) -> None:
    """Recurring background timer to check remote cloud storage for updates."""
    from backend_app.ingestion import remote_sync
    s = get_settings()
    while not stop_event.is_set():
        interval = max(30, s.docs_sync_interval_seconds)
        if stop_event.wait(timeout=interval):
            break
        if s.docs_sync_enabled and s.docs_remote_zip_url:
            try:
                remote_sync.sync_remote_zip(force=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("periodic_remote_sync_failed", error=str(exc))


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    setup_logging(s.service_name, s.log_level, s.mongo_uri if s.log_to_mongo else None, s.mongo_db)
    log.info("api_starting", config=s.public_summary())
    threading.Thread(target=_startup_tasks, daemon=True, name="startup").start()
    sync_stop = threading.Event()
    if s.docs_sync_enabled and s.docs_remote_zip_url:
        threading.Thread(target=_remote_sync_loop, args=(sync_stop,), daemon=True, name="remote_sync").start()
    backups.start()
    yield
    sync_stop.set()
    backups.stop()
    flush()


app = FastAPI(title="Hybrid RAG API", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    try:
        s = get_settings()
        if s.api_key and request.url.path not in ("/health", "/docs", "/openapi.json", "/version"):
            if request.headers.get("x-api-key") != s.api_key:
                return JSONResponse({"detail": "invalid or missing X-API-Key"}, status_code=401)
        if request.url.path not in ("/health", "/docs", "/openapi.json", "/version"):
            limit = check_rate_limit(
                request.url.path, client_host=request.client.host if request.client else None,
                api_key=request.headers.get("x-api-key"),
            )
            if not limit.allowed:
                return JSONResponse(
                    {"detail": "rate limit exceeded", "retry_after_s": limit.retry_after_s},
                    status_code=429,
                    headers={"Retry-After": str(limit.retry_after_s),
                             "X-RateLimit-Limit": str(limit.limit),
                             "X-RateLimit-Remaining": "0"},
                )
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        if request.url.path not in ("/health", "/docs", "/openapi.json", "/version"):
            response.headers["X-RateLimit-Limit"] = str(limit.limit)
            response.headers["X-RateLimit-Remaining"] = str(limit.remaining)
        if request.url.path not in ("/health", "/version"):
            log.info("http_request", method=request.method, path=request.url.path,
                     status=response.status_code,
                     ms=round((time.perf_counter() - started) * 1000, 1))
        return response
    finally:
        request_id_var.reset(token)


# --- models --------------------------------------------------------------------
Mode = Literal["hybrid", "dense", "bm25"]


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    conversation_id: str | None = None
    mode: Mode = "hybrid"
    top_k: int | None = Field(None, ge=1, le=20)
    weight_dense: float | None = Field(None, ge=0, le=1)
    source: str = "api"           # ui | mcp | eval | api  → Langfuse tag
    use_history: bool = True
    use_cache: bool = True        # false = skip the Redis caches for this request (eval does this)
    fast_mode: bool | None = None # None = use server default (fast); True = fast path (~1.8s); False = deep evaluation (~8s)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    mode: Mode = "hybrid"
    top_k: int | None = Field(None, ge=1, le=50)
    weight_dense: float | None = Field(None, ge=0, le=1)
    top_k_dense: int | None = Field(None, ge=1, le=50)
    top_k_bm25: int | None = Field(None, ge=1, le=50)
    source: str = "api"
    use_cache: bool = True


class LogEntry(BaseModel):
    ts: str | None = None
    level: str = "INFO"
    message: str = ""
    request_id: str | None = None
    trace_id: str | None = None
    extra: dict = Field(default_factory=dict)


class LogBatch(BaseModel):
    service: Literal["ui", "mcp", "eval"]     # the backend writes its own logs
    entries: list[LogEntry] = Field(..., max_length=500)


class TextDocument(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=500_000)


class ConflictActionRequest(BaseModel):
    source: str
    action: Literal["remove", "force", "copy"]
    new_name: str | None = None


class Feedback(BaseModel):
    trace_id: str
    value: Literal[0, 1]          # 1 = thumbs up, 0 = thumbs down
    comment: str | None = None


class EmbedTest(BaseModel):
    text: str = "Hello from the hybrid RAG"


# --- health & config ----------------------------------------------------------------
@app.get("/health")
def health():
    checks = {}
    for name, fn in [("mongodb", mongo.ping), ("qdrant", qdrant_store.ping),
                     ("redis", cache.ping), ("embeddings", check_embeddings), ("llm", check_llm),
                     ("langfuse", check_langfuse)]:
        ok, detail = fn()
        checks[name] = {"ok": ok, "detail": detail}
    core_ok = checks["mongodb"]["ok"] and checks["qdrant"]["ok"]
    return {"status": "ok" if core_ok else "degraded", "checks": checks,
            "bm25_chunks": bm25.size()}


@app.get("/version")
def version():
    s = get_settings()
    return {
        "app_version": s.app_version,
        "git_commit": s.git_commit,
        "build_date": s.build_date,
        "environment": s.environment,
    }


@app.get("/config")
def config():
    return get_settings().public_summary()


@app.get("/stats")
def stats():
    return {**repo.counts(), "qdrant_points": qdrant_store.count(), "bm25_chunks": bm25.size()}


@app.post("/embed-test")
def embed_test(body: EmbedTest):
    v = get_embedder().embed_query(body.text)
    return {"dim": len(v), "first_values": [round(x, 5) for x in v[:5]],
            "model": get_settings().embedding_model}


# --- documents & ingestion ------------------------------------------------------------
@app.post("/ingest/scan")
def ingest_scan(force: bool = False):
    """Ingest every .md/.txt in DOCS_DIR (unchanged files are skipped unless force=true)."""
    return pipeline.scan_docs_dir(trigger="api", force=force)


@app.post("/documents/upload")
async def upload_documents(files: list[UploadFile] = File(...)):
    import io
    import zipfile

    jobs = []
    for f in files:
        name = f.filename or "upload.md"
        raw = await f.read()
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    for member in z.infolist():
                        if not member.is_dir() and member.filename.lower().endswith((".md", ".txt")):
                            sub_name = member.filename
                            content = z.read(member).decode("utf-8", errors="replace")
                            jobs.append(pipeline.ingest_text(sub_name, content, trigger="zip_upload"))
            except Exception as exc:
                raise HTTPException(400, f"{name}: invalid zip file ({exc})")
        elif name.lower().endswith((".md", ".txt")):
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(400, f"{name}: file is not UTF-8 text")
            jobs.append(pipeline.ingest_text(name, content, trigger="upload"))
        else:
            raise HTTPException(400, f"{name}: only .md, .txt, and .zip files are supported")
    return {"jobs": jobs}


@app.post("/documents/sync-remote")
def trigger_remote_sync(force: bool = False, url: str | None = None):
    """Trigger on-demand sync from cloud storage with in-memory parallel folder decomposition."""
    from backend_app.ingestion import remote_sync
    return remote_sync.sync_remote_zip(url=url, force=force)


@app.get("/documents/sync-status")
def get_remote_sync_status():
    """Get real-time cloud sync telemetry, ETag, and folder space/time metrics."""
    from backend_app.ingestion import remote_sync
    return remote_sync.get_sync_status()


@app.post("/documents/text")
def add_text_document(body: TextDocument, source: str = "api"):
    return pipeline.ingest_text(body.name, body.content, trigger=source)


@app.get("/documents")
def list_documents():
    return repo.list_documents()


@app.get("/documents/{doc_id}")
def get_document(doc_id: str, include_content: bool = False):
    doc = repo.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "document not found")
    if not include_content:
        doc.pop("content", None)
    chunks = [{k: c[k] for k in ("chunk_id", "position", "heading_path", "text", "char_count")}
              for c in repo.get_chunks_for_doc(doc_id)]
    return {**doc, "chunks": chunks}


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str):
    if not pipeline.delete_document(doc_id):
        raise HTTPException(404, "document not found")
    return {"deleted": doc_id}


@app.post("/documents/{doc_id}/reingest")
def reingest_document(doc_id: str):
    try:
        return pipeline.reingest(doc_id)
    except KeyError:
        raise HTTPException(404, "document not found")


@app.post("/documents/conflict/action")
def resolve_document_conflict(body: ConflictActionRequest):
    """Resolves conflicts for existing/re-uploaded documents:
    - remove: removes file from pipeline & vector store
    - force: forces complete re-ingestion & re-embedding
    - copy: copies under new filename and ingests
    """
    if body.action == "remove":
        ok = pipeline.remove_document(body.source)
        if not ok:
            raise HTTPException(404, f"Document not found: {body.source}")
        return {"status": "removed", "source": body.source}
    elif body.action == "force":
        try:
            job = pipeline.force_ingest_document(body.source)
            return {"status": "force_ingested", "source": body.source, "job": job}
        except KeyError:
            raise HTTPException(404, f"Document not found: {body.source}")
    elif body.action == "copy":
        try:
            job = pipeline.copy_and_rename_document(body.source, new_name=body.new_name)
            new_source = (job.get("results") or [{}])[0].get("source") or body.new_name or f"{body.source}_copy"
            return {"status": "copied", "source": body.source, "new_source": new_source, "job": job}
        except KeyError:
            raise HTTPException(404, f"Document not found: {body.source}")
    else:
        raise HTTPException(400, f"Unsupported conflict action: {body.action}")



@app.get("/ingestion-jobs")
def ingestion_jobs(limit: int = 50):
    return repo.list_jobs(limit)


# --- retrieval & RAG -------------------------------------------------------------------------
@app.post("/search")
def search(body: SearchRequest):
    with trace_request("search", input=body.model_dump(),
                       tags=[f"source:{body.source}", f"mode:{body.mode}"]) as t:
        with t.span("hybrid_search", input={"query": body.query}, as_type="retriever") as sp:
            result = hybrid_search(body.query, body.mode, body.top_k, body.weight_dense,
                                   body.top_k_dense, body.top_k_bm25, body.use_cache)
            if sp is not None:
                sp.update(output={k: [(r["source"], r["chunk_id"][:8], round(r["score"], 4))
                                      for r in result[k]] for k in ("dense", "bm25", "fused")})
        trace_id = t.trace_id
    return {**result, "trace_id": trace_id, "trace_url": trace_url(trace_id)}


@app.post("/ask")
def ask(body: AskRequest):
    try:
        return rag_ask(body.question, body.conversation_id, body.mode, body.top_k,
                       body.weight_dense, body.source, body.use_history, body.use_cache,
                       fast_mode=body.fast_mode)
    except RuntimeError as exc:  # e.g. missing API keys
        log.error("ask_failed", error=str(exc))
        raise HTTPException(503, str(exc))
    except Exception as exc:
        log.error("ask_unhandled_exception", error=str(exc))
        raise HTTPException(500, f"RAG execution failed: {exc}")


@app.post("/ask/stream")
def ask_stream_endpoint(body: AskRequest):
    """Stream answer tokens from Gemini in real time with low TTFT (<300ms)."""
    from fastapi.responses import StreamingResponse
    from backend_app.graph.service import ask_stream
    return StreamingResponse(
        ask_stream(body.question, body.conversation_id, body.mode, body.top_k,
                   body.weight_dense, body.source, body.use_history, body.use_cache,
                   fast_mode=body.fast_mode),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/feedback")
def feedback(body: Feedback):
    ok = score(body.trace_id, "user_feedback", float(body.value), comment=body.comment)
    log.info("feedback", trace_id=body.trace_id, value=body.value, sent_to_langfuse=ok)
    return {"recorded": ok}


@app.get("/conversations")
def conversations(limit: int = 50):
    return repo.list_conversations(limit)


@app.get("/conversations/{conversation_id}/messages")
def conversation_messages(conversation_id: str):
    return repo.get_messages(conversation_id)


# --- logs & evaluation ---------------------------------------------------------------------
@app.get("/logs")
def logs(service: str | None = None, level: str | None = None,
         request_id: str | None = None, q: str | None = None,
         limit: int = Query(200, ge=1, le=1000)):
    rows = repo.query_logs(service, level, request_id, q, limit)
    for r in rows:
        r["trace_url"] = trace_url(r.get("trace_id"))
    return rows


@app.post("/logs/batch")
def logs_batch(body: LogBatch):
    """The frontend and MCP server send their logs here instead of writing to MongoDB."""
    return {"stored": repo.insert_log_batch(body.service, [e.model_dump() for e in body.entries])}


@app.post("/eval-runs")
def create_eval_run(run: dict):
    return {"run_id": repo.insert_eval_run(run)}


@app.get("/eval-runs")
def eval_runs(limit: int = 100):
    return repo.list_eval_runs(limit)


@app.get("/eval-runs/{run_id}")
def eval_run(run_id: str):
    run = repo.get_eval_run(run_id)
    if not run:
        raise HTTPException(404, "eval run not found")
    return run


# --- admin ------------------------------------------------------------------------------------
@app.post("/admin/qdrant/snapshot")
def qdrant_snapshot():
    """Write a snapshot to /qdrant/snapshots (= ./data/qdrant_snapshots on your PC)."""
    return qdrant_store.create_snapshot()


@app.get("/admin/qdrant/snapshots")
def qdrant_snapshots():
    return qdrant_store.list_snapshots()


@app.get("/admin/backups/status")
def backup_status():
    return backups.status()


@app.post("/admin/backups/qdrant")
def backup_qdrant_now():
    """Create a managed snapshot now and prune snapshots beyond QDRANT_SNAPSHOT_KEEP."""
    return backups.snapshot_once()


@app.get("/admin/cache/stats")
def cache_stats():
    return cache.stats()


@app.post("/admin/cache/clear")
def cache_clear(kind: Literal["answer", "search", "embedding", "health", "all"] = "all"):
    return cache.clear(kind)


@app.post("/admin/bm25/rebuild")
def rebuild_bm25():
    return {"chunks": bm25.rebuild()}
