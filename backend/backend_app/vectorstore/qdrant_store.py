"""Qdrant: one collection per embedding model, cosine distance.

Point id = chunk_id (a UUID derived from the chunk), payload = what we need to show a citation.
"""
from __future__ import annotations

from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from backend_app.config import get_settings
from backend_app.logging import get_logger

log = get_logger(__name__)


@lru_cache
def get_qdrant() -> QdrantClient:
    return QdrantClient(url=get_settings().qdrant_url, timeout=30)


def ensure_collection() -> None:
    s = get_settings()
    client = get_qdrant()
    if client.collection_exists(s.qdrant_collection):
        info = client.get_collection(s.qdrant_collection)
        size = info.config.params.vectors.size  # type: ignore[union-attr]
        if size != s.embedding_dim:
            raise RuntimeError(
                f"Collection '{s.qdrant_collection}' has {size}-dim vectors but EMBEDDING_DIM="
                f"{s.embedding_dim}. Use a new QDRANT_COLLECTION name for a different model.")
        return
    client.create_collection(
        s.qdrant_collection,
        vectors_config=qm.VectorParams(size=s.embedding_dim, distance=qm.Distance.COSINE),
    )
    client.create_payload_index(s.qdrant_collection, "doc_id", qm.PayloadSchemaType.KEYWORD)
    log.info("qdrant_collection_created", collection=s.qdrant_collection, dim=s.embedding_dim)


def upsert(points: list[tuple[str, list[float], dict]]) -> None:
    if not points:
        return
    get_qdrant().upsert(
        get_settings().qdrant_collection,
        points=[qm.PointStruct(id=pid, vector=vec, payload=payload) for pid, vec, payload in points],
        wait=True,
    )


def set_payloads(updates: list[tuple[str, dict]]) -> None:
    client, coll = get_qdrant(), get_settings().qdrant_collection
    for pid, payload in updates:
        client.set_payload(coll, payload=payload, points=[pid], wait=False)


def delete_points(point_ids: list[str]) -> None:
    if point_ids:
        get_qdrant().delete(get_settings().qdrant_collection,
                            points_selector=qm.PointIdsList(points=point_ids), wait=True)


def delete_doc(doc_id: str) -> None:
    get_qdrant().delete(
        get_settings().qdrant_collection,
        points_selector=qm.FilterSelector(filter=qm.Filter(must=[
            qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))])),
        wait=True,
    )


def search(vector: list[float], top_k: int) -> list[dict]:
    res = get_qdrant().query_points(get_settings().qdrant_collection, query=vector,
                                    limit=top_k, with_payload=True)
    return [{"chunk_id": str(p.id), "score": float(p.score), **(p.payload or {})}
            for p in res.points]


def count() -> int:
    s = get_settings()
    client = get_qdrant()
    if not client.collection_exists(s.qdrant_collection):
        return 0
    return client.count(s.qdrant_collection, exact=True).count


def create_snapshot() -> dict:
    snap = get_qdrant().create_snapshot(get_settings().qdrant_collection, wait=True)
    return {"name": snap.name, "size": snap.size, "created_at": snap.creation_time}


def list_snapshots() -> list[dict]:
    snaps = get_qdrant().list_snapshots(get_settings().qdrant_collection)
    return [{"name": s.name, "size": s.size, "created_at": s.creation_time} for s in snaps]


def delete_snapshot(name: str) -> None:
    """Delete one old snapshot after a newer scheduled snapshot has succeeded."""
    get_qdrant().delete_snapshot(get_settings().qdrant_collection, snapshot_name=name, wait=True)


def ping() -> tuple[bool, str]:
    try:
        get_qdrant().get_collections()
        return True, f"ok ({count()} points)"
    except Exception as exc:  # noqa: BLE001
        return False, f"unreachable: {exc.__class__.__name__}"
