"""Ingestion as a small LangGraph graph:  load → split → dedupe → embed → store.

Traced in Langfuse as one `ingest` trace per job with a span per node per file.
Dedupe rules:
  - same file hash as last time         → skip the file entirely (no API calls)
  - chunk already stored for this doc   → reuse its vector (no embedding call)
  - only brand-new chunks are embedded  → saves Hugging Face quota
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from backend_app.cache import store as cache
from backend_app.config import get_settings
from backend_app.db import repositories as repo
from backend_app.embeddings import get_embedder
from backend_app.ingestion.splitter import doc_id_for, doc_title, file_hash, split_document
from backend_app.logging import get_logger
from backend_app.retrieval import bm25
from backend_app.tracing import trace_request
from backend_app.vectorstore import qdrant_store

log = get_logger(__name__)
_ingest_lock = threading.Lock()
ALLOWED_SUFFIXES = {".md", ".txt"}


class IngestState(TypedDict, total=False):
    source: str
    path: str | None
    content: str | None
    origin: str
    force: bool
    doc_id: str
    file_hash: str
    skipped: bool
    chunks: list[dict]
    to_embed: list[dict]
    reused: list[dict]
    removed_ids: list[str]
    result: dict


# --- nodes ------------------------------------------------------------------
def load(state: IngestState) -> IngestState:
    content = state.get("content")
    if content is None:
        content = Path(state["path"]).read_text(encoding="utf-8")
    content = content.replace("\r\n", "\n")
    return {"content": content, "doc_id": doc_id_for(state["source"]),
            "file_hash": file_hash(content)}


def split(state: IngestState) -> IngestState:
    s = get_settings()
    existing = repo.get_document_by_source(state["source"])
    # Skip only if the file is unchanged AND was embedded with the current provider + model.
    if (not state.get("force") and existing and existing.get("file_hash") == state["file_hash"]
            and existing.get("status") == "ingested"
            and existing.get("embedding_provider") == s.embedding_provider
            and existing.get("embedding_model") == s.embedding_model):
        return {"skipped": True, "chunks": []}
    return {"skipped": False,
            "chunks": split_document(state["source"], state["content"], s.chunk_size,
                                     s.chunk_overlap)}


def dedupe(state: IngestState) -> IngestState:
    if state.get("skipped"):
        return {"to_embed": [], "reused": [], "removed_ids": []}
    s = get_settings()
    existing_ids = repo.get_chunk_ids_for_doc(state["doc_id"])
    new_ids = {c["chunk_id"] for c in state["chunks"]}
    # A vector can be reused only if it came from the same provider + model. `force` reuses nothing.
    reusable = (set() if state.get("force") else
                repo.get_chunk_ids_embedded_with(state["doc_id"], s.embedding_provider,
                                                 s.embedding_model))
    return {
        "to_embed": [c for c in state["chunks"] if c["chunk_id"] not in reusable],
        "reused": [c for c in state["chunks"] if c["chunk_id"] in reusable],
        "removed_ids": sorted(existing_ids - new_ids),
    }


def embed_and_store(state: IngestState) -> IngestState:
    s = get_settings()
    if state.get("skipped"):
        return {"result": {"source": state["source"], "status": "unchanged", "chunks_new": 0}}

    to_embed = state["to_embed"]
    vectors = get_embedder().embed_documents([c["index_text"] for c in to_embed]) if to_embed else []

    def payload(c: dict) -> dict:
        return {k: c[k] for k in ("doc_id", "source", "heading_path", "position", "chunk_hash")}

    qdrant_store.upsert([(c["chunk_id"], v, payload(c)) for c, v in zip(to_embed, vectors)])
    qdrant_store.set_payloads([(c["chunk_id"], payload(c)) for c in state["reused"]])
    qdrant_store.delete_points(state["removed_ids"])

    for c in state["chunks"]:  # remember which model made each vector (see dedupe)
        c["embedding_provider"] = s.embedding_provider
        c["embedding_model"] = s.embedding_model
    repo.replace_chunks(state["doc_id"], state["chunks"])
    repo.upsert_document({
        "doc_id": state["doc_id"], "source": state["source"],
        "name": Path(state["source"]).name,
        "title": doc_title(state["source"], state["content"]),
        "file_hash": state["file_hash"], "size": len(state["content"]),
        "content": state["content"], "chunk_count": len(state["chunks"]),
        "origin": state.get("origin", "file"), "status": "ingested",
        "embedding_provider": s.embedding_provider, "embedding_model": s.embedding_model,
        "ingested_at": datetime.now(timezone.utc),
    })
    return {"result": {
        "source": state["source"], "status": "ingested", "chunks": len(state["chunks"]),
        "chunks_new": len(to_embed), "chunks_reused": len(state["reused"]),
        "chunks_removed": len(state["removed_ids"]),
    }}


def _build_graph():
    g = StateGraph(IngestState)
    g.add_node("load", load)
    g.add_node("split", split)
    g.add_node("dedupe", dedupe)
    g.add_node("embed_and_store", embed_and_store)
    g.add_edge(START, "load")
    g.add_edge("load", "split")
    g.add_edge("split", "dedupe")
    g.add_edge("dedupe", "embed_and_store")
    g.add_edge("embed_and_store", END)
    return g.compile()


_graph = None


def graph():
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


# --- public functions ----------------------------------------------------------
def ingest_items(items: list[dict], trigger: str, prune_missing: bool = False) -> dict:
    """items: [{"source", "path"?, "content"?, "origin"?, "force"?}]"""
    with _ingest_lock:
        started = time.perf_counter()
        job_id = repo.create_job([i["source"] for i in items], trigger)
        results, errors = [], []
        with trace_request("ingest", input={"files": [i["source"] for i in items],
                                            "trigger": trigger},
                           tags=[f"trigger:{trigger}"], metadata={"job_id": job_id}) as t:
            handler = t.callback_handler()
            config = {"callbacks": [handler]} if handler else {}
            qdrant_store.ensure_collection()
            for item in items:
                try:
                    out = graph().invoke(item, config=config)
                    results.append(out["result"])
                except Exception as exc:  # noqa: BLE001
                    log.error("ingest_failed", source=item["source"], error=str(exc))
                    errors.append(f"{item['source']}: {exc}")
            if prune_missing:
                present = {i["source"] for i in items}
                for doc in repo.list_documents():
                    if doc.get("origin", "file") == "file" and doc["source"] not in present:
                        delete_document(doc["doc_id"], rebuild=False)
                        results.append({"source": doc["source"], "status": "removed",
                                        "chunks_new": 0})
            bm25.rebuild()
            if any(r.get("status") != "unchanged" for r in results):
                cache.bump_index_version()  # old cached searches/answers are no longer matched
            t.update(output={"results": results, "errors": errors})
            trace_id = t.trace_id
        duration = time.perf_counter() - started
        repo.finish_job(job_id, results, errors, duration, trace_id)
        log.info("ingest_done", job_id=job_id, files=len(items), errors=len(errors),
                 duration_s=round(duration, 2))
        return repo.get_job(job_id) or {"job_id": job_id}


def scan_docs_dir(trigger: str = "scan", force: bool = False) -> dict:
    root = Path(get_settings().docs_dir)
    items = []
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES:
                items.append({"source": p.relative_to(root).as_posix(), "path": str(p),
                              "origin": "file", "force": force})
    return ingest_items(items, trigger, prune_missing=True)


def ingest_text(name: str, content: str, trigger: str) -> dict:
    """Upload / MCP: save under docs_dir/uploads when possible so it is visible on disk."""
    safe = Path(name).name
    if Path(safe).suffix.lower() not in ALLOWED_SUFFIXES:
        safe += ".md"
    source = f"uploads/{safe}"
    origin = "upload"
    try:
        target = Path(get_settings().docs_dir) / "uploads" / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        origin = "file"
    except OSError as exc:
        log.warning("upload_not_saved_to_disk", source=source, error=str(exc))
    return ingest_items([{"source": source, "content": content, "origin": origin}], trigger)


def reingest(doc_id: str) -> dict:
    doc = repo.get_document_raw(doc_id)
    if not doc:
        raise KeyError(doc_id)
    path = Path(get_settings().docs_dir) / doc["source"]
    item = {"source": doc["source"], "origin": doc.get("origin", "file"), "force": True}
    if path.exists():
        item["path"] = str(path)
    else:
        item["content"] = doc.get("content", "")
    return ingest_items([item], "reingest")


def delete_document(doc_id: str, rebuild: bool = True) -> bool:
    doc = repo.get_document_raw(doc_id)
    if not doc:
        return False
    qdrant_store.delete_doc(doc_id)
    repo.delete_document(doc_id)
    # Uploaded files are removed from disk too, otherwise the next scan would re-add them.
    # Files you placed in data/docs yourself are left alone: delete them on disk instead.
    if doc["source"].startswith("uploads/"):
        try:
            (Path(get_settings().docs_dir) / doc["source"]).unlink(missing_ok=True)
        except OSError:
            pass
    if rebuild:
        bm25.rebuild()
        cache.bump_index_version()
    log.info("document_deleted", doc_id=doc_id, source=doc["source"])
    return True
