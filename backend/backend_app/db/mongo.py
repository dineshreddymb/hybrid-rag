"""MongoDB client (one per process) and index setup."""
from __future__ import annotations

from functools import lru_cache

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database

from backend_app.config import get_settings


@lru_cache
def get_client() -> MongoClient:
    s = get_settings()
    return MongoClient(s.mongo_uri, serverSelectionTimeoutMS=3000, tz_aware=True)


def get_db() -> Database:
    return get_client()[get_settings().mongo_db]


def ensure_indexes() -> None:
    s = get_settings()
    db = get_db()
    db.documents.create_index([("source", ASCENDING)], unique=True)
    db.chunks.create_index([("doc_id", ASCENDING), ("position", ASCENDING)])
    db.chunks.create_index([("chunk_id", ASCENDING)], unique=True)
    db.ingestion_jobs.create_index([("started_at", DESCENDING)])
    db.conversations.create_index([("updated_at", DESCENDING)])
    db.messages.create_index([("conversation_id", ASCENDING), ("created_at", ASCENDING)])
    db.eval_runs.create_index([("created_at", DESCENDING)])
    db.app_logs.create_index([("service", ASCENDING), ("level", ASCENDING), ("ts", DESCENDING)])
    db.app_logs.create_index([("request_id", ASCENDING)])
    # TTL: Mongo deletes log rows automatically after LOG_TTL_DAYS.
    ttl_seconds = s.log_ttl_days * 24 * 3600
    existing = {i["name"]: i for i in db.app_logs.list_indexes()}
    ttl = existing.get("ts_ttl")
    if ttl and ttl.get("expireAfterSeconds") != ttl_seconds:
        db.app_logs.drop_index("ts_ttl")
    db.app_logs.create_index([("ts", ASCENDING)], name="ts_ttl", expireAfterSeconds=ttl_seconds)


def ping() -> tuple[bool, str]:
    try:
        get_client().admin.command("ping")
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"unreachable: {exc.__class__.__name__}"
