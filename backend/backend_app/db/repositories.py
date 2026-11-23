"""Small data-access functions. Keeping all Mongo queries here makes them easy to find."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import DESCENDING

from backend_app.db.mongo import get_db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    for k, v in doc.items():
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


# --- documents ------------------------------------------------------------
def get_document_by_source(source: str) -> dict | None:
    return get_db().documents.find_one({"source": source})


def get_document(doc_id: str) -> dict | None:
    return _clean(get_db().documents.find_one({"doc_id": doc_id}))


def get_document_raw(doc_id: str) -> dict | None:
    return get_db().documents.find_one({"doc_id": doc_id})


def upsert_document(doc: dict) -> None:
    doc = {**doc, "updated_at": _now()}
    get_db().documents.update_one({"source": doc["source"]}, {"$set": doc}, upsert=True)


def list_documents() -> list[dict]:
    cur = get_db().documents.find({}, {"content": 0}).sort("source", 1)
    return [_clean(d) for d in cur]


def delete_document(doc_id: str) -> None:
    db = get_db()
    db.documents.delete_one({"doc_id": doc_id})
    db.chunks.delete_many({"doc_id": doc_id})


# --- chunks ---------------------------------------------------------------
def get_chunks_for_doc(doc_id: str) -> list[dict]:
    return [_clean(c) for c in get_db().chunks.find({"doc_id": doc_id}).sort("position", 1)]


def get_chunk_ids_for_doc(doc_id: str) -> set[str]:
    return {c["chunk_id"] for c in get_db().chunks.find({"doc_id": doc_id}, {"chunk_id": 1})}


def get_chunk_ids_embedded_with(doc_id: str, provider: str, model: str) -> set[str]:
    """Chunk ids whose stored vector was made by this provider + model (so it can be reused).
    Chunks stored before this tag existed have no tag and count as 'not matching'."""
    cur = get_db().chunks.find({"doc_id": doc_id, "embedding_provider": provider,
                                "embedding_model": model}, {"chunk_id": 1})
    return {c["chunk_id"] for c in cur}


def replace_chunks(doc_id: str, chunks: list[dict]) -> None:
    db = get_db()
    db.chunks.delete_many({"doc_id": doc_id})
    if chunks:
        db.chunks.insert_many([dict(c) for c in chunks])


def all_chunks() -> list[dict]:
    return [_clean(c) for c in get_db().chunks.find({})]


def get_chunks_by_ids(chunk_ids: list[str]) -> dict[str, dict]:
    if not chunk_ids:
        return {}
    cur = get_db().chunks.find({"chunk_id": {"$in": chunk_ids}})
    return {c["chunk_id"]: _clean(c) for c in cur}


# --- ingestion jobs -------------------------------------------------------
def create_job(files: list[str], trigger: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    get_db().ingestion_jobs.insert_one({
        "job_id": job_id, "files": files, "trigger": trigger, "status": "running",
        "started_at": _now(), "results": [], "errors": [],
    })
    return job_id


def finish_job(job_id: str, results: list[dict], errors: list[str], duration_s: float,
               trace_id: str | None) -> None:
    get_db().ingestion_jobs.update_one({"job_id": job_id}, {"$set": {
        "status": "failed" if errors and not results else ("partial" if errors else "done"),
        "results": results, "errors": errors, "duration_s": round(duration_s, 3),
        "finished_at": _now(), "trace_id": trace_id,
        "chunks_created": sum(r.get("chunks_new", 0) for r in results),
    }})


def list_jobs(limit: int = 50) -> list[dict]:
    cur = get_db().ingestion_jobs.find({}).sort("started_at", DESCENDING).limit(limit)
    return [_clean(j) for j in cur]


def get_job(job_id: str) -> dict | None:
    return _clean(get_db().ingestion_jobs.find_one({"job_id": job_id}))


# --- conversations & messages ---------------------------------------------
def ensure_conversation(conversation_id: str | None, first_question: str) -> str:
    db = get_db()
    if conversation_id and db.conversations.find_one({"conversation_id": conversation_id}):
        db.conversations.update_one({"conversation_id": conversation_id},
                                    {"$set": {"updated_at": _now()}})
        return conversation_id
    conversation_id = conversation_id or uuid.uuid4().hex
    db.conversations.insert_one({
        "conversation_id": conversation_id, "title": first_question[:80],
        "created_at": _now(), "updated_at": _now(),
    })
    return conversation_id


def add_message(conversation_id: str, role: str, content: str, **extra: Any) -> None:
    get_db().messages.insert_one({
        "conversation_id": conversation_id, "role": role, "content": content,
        "created_at": _now(), **extra,
    })


def recent_messages(conversation_id: str, limit: int) -> list[dict]:
    cur = (get_db().messages.find({"conversation_id": conversation_id},
                                  {"role": 1, "content": 1, "created_at": 1})
           .sort("created_at", DESCENDING).limit(limit))
    return [_clean(m) for m in reversed(list(cur))]


def list_conversations(limit: int = 50) -> list[dict]:
    cur = get_db().conversations.find({}).sort("updated_at", DESCENDING).limit(limit)
    return [_clean(c) for c in cur]


def get_messages(conversation_id: str) -> list[dict]:
    cur = get_db().messages.find({"conversation_id": conversation_id}).sort("created_at", 1)
    return [_clean(m) for m in cur]


# --- logs -----------------------------------------------------------------
def query_logs(service: str | None, level: str | None, request_id: str | None,
               text: str | None, limit: int) -> list[dict]:
    q: dict[str, Any] = {}
    if service:
        q["service"] = service
    if level:
        q["level"] = level.upper()
    if request_id:
        q["request_id"] = request_id
    if text:
        q["message"] = {"$regex": text, "$options": "i"}
    cur = get_db().app_logs.find(q).sort("ts", DESCENDING).limit(min(limit, 1000))
    return [_clean(r) for r in cur]


def insert_log_batch(service: str, entries: list[dict]) -> int:
    """Log rows shipped by the frontend / MCP server (they have no database access themselves)."""
    docs = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(str(e.get("ts")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            ts = _now()
        docs.append({"ts": ts, "level": str(e.get("level") or "INFO").upper(), "service": service,
                     "message": str(e.get("message", ""))[:2000], "request_id": e.get("request_id"),
                     "trace_id": e.get("trace_id"), "extra": e.get("extra") or {}})
    if docs:
        get_db().app_logs.insert_many(docs, ordered=False)
    return len(docs)


# --- eval runs ------------------------------------------------------------
def insert_eval_run(run: dict) -> str:
    run_id = run.get("run_id") or uuid.uuid4().hex[:12]
    get_db().eval_runs.insert_one({**run, "run_id": run_id, "created_at": _now()})
    return run_id


def list_eval_runs(limit: int = 100) -> list[dict]:
    cur = get_db().eval_runs.find({}, {"items": 0}).sort("created_at", DESCENDING).limit(limit)
    return [_clean(r) for r in cur]


def get_eval_run(run_id: str) -> dict | None:
    return _clean(get_db().eval_runs.find_one({"run_id": run_id}))


def counts() -> dict:
    db = get_db()
    return {
        "documents": db.documents.count_documents({}),
        "chunks": db.chunks.count_documents({}),
        "conversations": db.conversations.count_documents({}),
        "eval_runs": db.eval_runs.count_documents({}),
    }
