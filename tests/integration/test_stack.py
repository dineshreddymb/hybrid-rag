"""Integration tests against the RUNNING containers (podman-compose up -d).

    .venv/Scripts/python -m pytest tests/integration -q                 # API + UI checks
    mcp-server/.venv/Scripts/python -m pytest tests/integration -q    # also runs the MCP test

Skipped automatically when the API is not reachable. They work with any provider; with the
fake providers they cost nothing, with real keys each /ask uses a few Gemini calls.
"""
import asyncio
import os
import uuid

import httpx
import pytest

API = os.environ.get("API_URL", "http://localhost:8000")
UI = os.environ.get("UI_URL", "http://localhost:8050")
MCP = os.environ.get("MCP_URL", "http://localhost:8100/mcp")


def _up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _up(f"{API}/config"), reason="API not running")
client = httpx.Client(base_url=API, timeout=180)


def test_health_core_services_ok():
    h = client.get("/health").json()
    assert h["checks"]["mongodb"]["ok"] and h["checks"]["qdrant"]["ok"], h
    assert h["bm25_chunks"] > 0


def test_counts_match_between_mongo_qdrant_bm25():
    s = client.get("/stats").json()
    assert s["chunks"] == s["qdrant_points"] == s["bm25_chunks"] > 0, s
    assert s["chunks"] <= 100


def test_rescan_is_idempotent():
    job = client.post("/ingest/scan").json()
    assert job["chunks_created"] == 0, job
    assert all(r["status"] == "unchanged" for r in job["results"]), job["results"]


def test_bm25_finds_exact_error_code():
    r = client.post("/search", json={"query": "E-271", "mode": "bm25", "top_k": 3}).json()
    assert "E-271" in r["fused"][0]["text"]


def test_hybrid_search_returns_three_lists():
    r = client.post("/search", json={"query": "parental leave weeks", "top_k": 5}).json()
    assert r["dense"] and r["bm25"] and r["fused"]
    assert all("rank" in f for f in r["fused"])


def test_ask_returns_answer_citations_and_conversation():
    r = client.post("/ask", json={"question": "What does error code E-217 mean?",
                                  "source": "api"}).json()
    assert r["answer"] and r["conversation_id"]
    assert r["citations"] and r["citations"][0]["source"]
    msgs = client.get(f"/conversations/{r['conversation_id']}/messages").json()
    assert [m["role"] for m in msgs][-2:] == ["user", "assistant"]


def test_upload_search_delete_roundtrip():
    word = f"zebra{uuid.uuid4().hex[:6]}"
    name = f"itest-{word}.md"
    job = client.post("/documents/text", json={"name": name,
                                               "content": f"# T\n\n## S\n\nmagic {word}"}).json()
    assert job["status"] == "done", job
    hit = client.post("/search", json={"query": word, "mode": "bm25", "top_k": 1}).json()
    assert hit["fused"][0]["source"] == f"uploads/{name}"
    doc_id = hit["fused"][0]["doc_id"]
    assert client.delete(f"/documents/{doc_id}").status_code == 200
    assert client.get(f"/documents/{doc_id}").status_code == 404


def test_logs_are_written_to_mongo():
    rows = client.get("/logs", params={"service": "api", "limit": 5}).json()
    assert rows and rows[0]["service"] == "api"


@pytest.mark.skipif(not _up(f"{UI}/healthz"), reason="UI not running")
def test_ui_serves_pages():
    for path in ("/", "/_dash-layout", "/_dash-dependencies"):
        assert httpx.get(f"{UI}{path}", timeout=10).status_code == 200, path


@pytest.mark.skipif(not _up(MCP.rsplit("/", 1)[0] + "/health"), reason="MCP not running")
def test_mcp_tools_over_streamable_http():
    mcp_client = pytest.importorskip("mcp.client.client")

    async def run():
        async with mcp_client.Client(MCP) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            assert {"ask", "search", "list_documents", "ingest_text", "get_document"} <= tools
            res = await c.call_tool("list_documents", {})
            assert not res.is_error
            res = await c.call_tool("search", {"query": "E-217", "mode": "bm25", "top_k": 2})
            assert not res.is_error

    asyncio.run(run())


# --- v4: Redis cache, log shipping, tier isolation (MASTER_PLAN §15) -----------------------------
import shutil
import subprocess
import time


def _podman_exec(container: str, code: str) -> subprocess.CompletedProcess:
    return subprocess.run(["podman", "exec", container, "python", "-c", code],
                          capture_output=True, text=True, timeout=60)


def _container_running(name: str) -> bool:
    if not shutil.which("podman"):
        return False
    out = subprocess.run(["podman", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    return name in out.stdout.split()


def _cache_available() -> bool:
    try:
        return bool(client.get("/admin/cache/stats").json().get("available", False))
    except httpx.HTTPError:
        return False


def test_health_reports_redis():
    assert "redis" in client.get("/health").json()["checks"]


@pytest.mark.skipif(not _cache_available(), reason="Redis cache not available")
def test_second_identical_question_is_served_from_cache():
    q = f"What does error code E-218 mean? ({uuid.uuid4().hex[:6]})"   # unique: first call is a miss
    first = client.post("/ask", json={"question": q}).json()
    second = client.post("/ask", json={"question": q}).json()
    assert first["cached"] is False and second["cached"] is True
    assert second["answer"] == first["answer"]
    assert second["conversation_id"] != first["conversation_id"]       # each ask is its own chat
    # bypass: use_cache=false recomputes
    third = client.post("/ask", json={"question": q, "use_cache": False}).json()
    assert third["cached"] is False


@pytest.mark.skipif(not _cache_available(), reason="Redis cache not available")
def test_upload_invalidates_cached_answers():
    q = f"How many SK-200 nodes fit one G3? ({uuid.uuid4().hex[:6]})"
    client.post("/ask", json={"question": q})
    assert client.post("/ask", json={"question": q}).json()["cached"] is True
    name = f"itest-cache-{uuid.uuid4().hex[:6]}.md"
    client.post("/documents/text", json={"name": name, "content": "# T\n\n## S\n\nunrelated note"})
    try:
        assert client.post("/ask", json={"question": q}).json()["cached"] is False
    finally:
        docs = client.get("/documents").json()
        for d in docs:
            if d["source"] == f"uploads/{name}":
                client.delete(f"/documents/{d['doc_id']}")


def test_search_cache_and_admin_endpoints():
    if not _cache_available():
        pytest.skip("Redis cache not available")
    q = f"parental leave {uuid.uuid4().hex[:6]}"
    a = client.post("/search", json={"query": q}).json()
    b = client.post("/search", json={"query": q}).json()
    assert a["cached"] is False and b["cached"] is True
    stats = client.get("/admin/cache/stats").json()
    assert set(stats["kinds"]) == {"embedding", "search", "answer", "health"}
    assert stats["kinds"]["search"]["hits"] >= 1 and stats["index_version"] >= 1
    cleared = client.post("/admin/cache/clear", params={"kind": "search"}).json()
    assert cleared["cleared"]["search"] >= 1
    assert client.post("/search", json={"query": q}).json()["cached"] is False


@pytest.mark.skipif(not _container_running("rag-frontend"), reason="frontend container not running")
def test_frontend_logs_arrive_via_backend():
    marker = uuid.uuid4().hex[:8]
    code = ("import logging, time; from frontend_app.logging_setup import setup_logging; "
            f"setup_logging(); logging.getLogger('ui').info('itest-{marker}'); time.sleep(4)")
    res = _podman_exec("rag-frontend", code)
    assert res.returncode == 0, res.stderr
    rows = client.get("/logs", params={"service": "ui", "q": f"itest-{marker}"}).json()
    assert rows and rows[0]["service"] == "ui"


@pytest.mark.skipif(not _container_running("rag-frontend"), reason="frontend container not running")
def test_frontend_tier_cannot_reach_data_stores():
    probe = ("import socket, sys\n"
             "for host in sys.argv[1:] or ['mongodb','qdrant','redis']:\n"
             "    try:\n        socket.gethostbyname(host); print('RESOLVED', host)\n"
             "    except OSError:\n        print('unresolved', host)\n")
    res = _podman_exec("rag-frontend", probe)
    assert "RESOLVED" not in res.stdout, res.stdout
    ok = _podman_exec("rag-frontend", "import socket; socket.gethostbyname('backend'); print('backend-ok')")
    assert "backend-ok" in ok.stdout, ok.stderr


@pytest.mark.skipif(not _container_running("rag-mcp"), reason="mcp container not running")
def test_mcp_tier_cannot_reach_data_stores():
    res = _podman_exec("rag-mcp", "import socket\n"
                       "for h in ('mongodb','qdrant','redis'):\n"
                       "    try:\n        socket.gethostbyname(h); print('RESOLVED', h)\n"
                       "    except OSError:\n        pass\n")
    assert "RESOLVED" not in res.stdout, res.stdout
