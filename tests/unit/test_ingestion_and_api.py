"""Ingestion re-embedding rules and the new log-shipping endpoint (no services needed)."""
import pytest
from fastapi.testclient import TestClient

from backend_app.config import get_settings
from backend_app.ingestion import pipeline
from backend_app.main import app


# --- re-embed when the provider / model changes (MASTER_PLAN §15.7 step A fixes) -------------------
@pytest.fixture
def stored(monkeypatch):
    """Pretend one document was ingested earlier with embedding provider `fake`."""
    s = get_settings()
    doc = {"file_hash": "H", "status": "ingested", "embedding_provider": "fake",
           "embedding_model": s.embedding_model}
    monkeypatch.setattr(s, "embedding_provider", "fake")
    monkeypatch.setattr(pipeline.repo, "get_document_by_source", lambda source: doc)
    monkeypatch.setattr(pipeline.repo, "get_chunk_ids_for_doc", lambda doc_id: {"c1", "c2"})
    monkeypatch.setattr(pipeline.repo, "get_chunk_ids_embedded_with",
                        lambda doc_id, provider, model:
                        {"c1", "c2"} if (provider, model) == ("fake", s.embedding_model) else set())
    return doc


def test_unchanged_file_with_same_provider_is_skipped(stored):
    state = {"source": "a.md", "content": "x", "file_hash": "H"}
    assert pipeline.split(state)["skipped"] is True


def test_unchanged_file_is_reprocessed_when_provider_changes(stored, monkeypatch):
    monkeypatch.setattr(get_settings(), "embedding_provider", "hf_api")
    state = {"source": "a.md", "content": "# T\n\ntext", "file_hash": "H"}
    assert pipeline.split(state)["skipped"] is False


def test_chunks_are_reused_only_for_the_same_provider(stored, monkeypatch):
    chunks = [{"chunk_id": "c1"}, {"chunk_id": "c2"}, {"chunk_id": "c3"}]
    state = {"doc_id": "d", "chunks": chunks}
    out = pipeline.dedupe(state)
    assert [c["chunk_id"] for c in out["reused"]] == ["c1", "c2"]
    assert [c["chunk_id"] for c in out["to_embed"]] == ["c3"]

    monkeypatch.setattr(get_settings(), "embedding_provider", "hf_api")   # switched to real vectors
    out = pipeline.dedupe(state)
    assert out["reused"] == [] and len(out["to_embed"]) == 3


def test_force_re_embeds_everything(stored):
    chunks = [{"chunk_id": "c1"}, {"chunk_id": "c2"}]
    out = pipeline.dedupe({"doc_id": "d", "chunks": chunks, "force": True})
    assert out["reused"] == [] and len(out["to_embed"]) == 2


# --- POST /logs/batch ---------------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    sent = []
    monkeypatch.setattr("backend_app.main.repo.insert_log_batch",
                        lambda service, entries: sent.append((service, entries)) or len(entries))
    return TestClient(app), sent   # no `with`: skips the lifespan (no Mongo/Qdrant needed)


def test_logs_batch_stores_entries(client):
    c, sent = client
    body = {"service": "ui", "entries": [{"ts": "2026-11-18T10:00:00Z", "level": "INFO",
                                          "message": "api_call", "request_id": "r1",
                                          "extra": {"path": "/ask"}}]}
    r = c.post("/logs/batch", json=body)
    assert r.status_code == 200 and r.json() == {"stored": 1}
    assert sent[0][0] == "ui" and sent[0][1][0]["request_id"] == "r1"


def test_logs_batch_rejects_unknown_service_and_oversized_batches(client):
    c, sent = client
    assert c.post("/logs/batch", json={"service": "api", "entries": []}).status_code == 422
    too_many = {"service": "mcp", "entries": [{"message": "x"}] * 501}
    assert c.post("/logs/batch", json=too_many).status_code == 422
    assert not sent


def test_ask_and_search_accept_use_cache_flag(client, monkeypatch):
    c, _ = client
    seen = {}
    monkeypatch.setattr("backend_app.main.rag_ask",
                        lambda *a, **kw: seen.setdefault("ask", a) and {"answer": "ok"})
    r = c.post("/ask", json={"question": "q", "use_cache": False})
    assert r.status_code == 200 and seen["ask"][-1] is False


def test_embed_and_store_marks_duplicate_when_skipped(stored):
    state = {"source": "a.md", "content": "x", "file_hash": "H", "skipped": True}
    out = pipeline.embed_and_store(state)
    assert out["result"]["status"] == "unchanged"
    assert out["result"]["is_duplicate"] is True
    assert out["result"]["chunks_new"] == 0


def test_conflict_actions_endpoint(client, monkeypatch):
    c, _ = client
    # Mock pipeline functions
    monkeypatch.setattr("backend_app.ingestion.pipeline.remove_document", lambda src: src == "test.md")
    monkeypatch.setattr("backend_app.ingestion.pipeline.force_ingest_document", lambda src: {"job_id": "j_force", "source": src})
    monkeypatch.setattr("backend_app.ingestion.pipeline.copy_and_rename_document",
                        lambda src, new_name=None: {"job_id": "j_copy", "results": [{"source": new_name or f"{src}_copy"}]})

    # Action 1: Remove
    r_rem = c.post("/documents/conflict/action", json={"source": "test.md", "action": "remove"})
    assert r_rem.status_code == 200
    assert r_rem.json() == {"status": "removed", "source": "test.md"}

    # Action 2: Force
    r_force = c.post("/documents/conflict/action", json={"source": "test.md", "action": "force"})
    assert r_force.status_code == 200
    assert r_force.json()["status"] == "force_ingested"

    # Action 3: Copy
    r_copy = c.post("/documents/conflict/action", json={"source": "test.md", "action": "copy"})
    assert r_copy.status_code == 200
    assert r_copy.json()["status"] == "copied"
    assert "copy" in r_copy.json()["new_source"]

