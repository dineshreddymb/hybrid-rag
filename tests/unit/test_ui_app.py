"""Unit tests for the Dash UI: pages register, layouts build, callbacks work against a fake API.

The UI package is `frontend_app` (not `app`) so it never clashes with the backend's `backend_app` package.
"""
import base64
import json
import sys
from contextvars import copy_context
from pathlib import Path

import httpx
import plotly
import pytest

pytest.importorskip("dash")  # skip cleanly when run from a venv without the UI deps

UI_DIR = Path(__file__).resolve().parents[2] / "frontend"
if str(UI_DIR) not in sys.path:
    sys.path.insert(0, str(UI_DIR))

import dash  # noqa: E402
from dash._callback_context import context_value  # noqa: E402
from dash._utils import AttributeDict  # noqa: E402

from frontend_app import api_client  # noqa: E402
from frontend_app.main import app, server  # noqa: E402
from frontend_app.pages import chat, documents, evaluation, logs, retrieval, settings  # noqa: E402

EXPECTED_PATHS = {"/": "Chat", "/documents": "Documents", "/retrieval": "Retrieval Debug",
                  "/evaluation": "Evaluation", "/logs": "Logs", "/settings": "Settings / Health"}

ROW = {"chunk_id": "c1", "score": 0.9, "source": "a.md", "heading_path": "A > B",
       "position": 0, "doc_id": "d1", "text": "alpha beta"}
FAKE = {
    ("GET", "/health"): {"status": "degraded", "bm25_chunks": 3, "checks": {
        "mongodb": {"ok": True, "detail": "ok"}, "qdrant": {"ok": True, "detail": "ok"},
        "embeddings": {"ok": False, "detail": "HF_TOKEN missing"},
        "llm": {"ok": True, "detail": "fake"}, "langfuse": {"ok": False, "detail": "disabled"}}},
    ("GET", "/config"): {"top_k_final": 5, "hybrid_weight_dense": 0.5, "top_k_dense": 20,
                         "top_k_bm25": 20, "langfuse_enabled": False},
    ("GET", "/stats"): {"documents": 1, "chunks": 3},
    ("POST", "/ask"): {"answer": "Alpha [1].", "conversation_id": "conv1",
                       "rewritten_query": "what is alpha", "not_found": False, "grounded": True,
                       "grounding_reason": "ok", "mode": "hybrid", "latency_ms": 12,
                       "trace_id": "t1", "trace_url": "http://localhost:3000/trace/t1",
                       "citations": [{"n": 1, "chunk_id": "c1", "source": "a.md",
                                      "heading_path": "A", "text": "alpha", "cited": True}]},
    ("POST", "/search"): {"query": "q", "mode": "hybrid", "weight_dense": 0.5, "top_k": 5,
                          "dense": [ROW], "bm25": [dict(ROW, chunk_id="c2")],
                          "fused": [dict(ROW, rank=1, dense_rank=1, bm25_rank=None),
                                    dict(ROW, chunk_id="c2", rank=2, dense_rank=None,
                                         bm25_rank=1)],
                          "timings": {"dense_ms": 3, "bm25_ms": 1}, "trace_id": None,
                          "trace_url": None},
    ("POST", "/feedback"): {"recorded": False},
    ("GET", "/admin/cache/stats"): {
        "enabled": True, "available": True, "memory_used": "1.2M", "memory_max": "256.00M",
        "index_version": 7,
        "kinds": {"embedding": {"hits": 5, "misses": 5, "keys": 10, "hit_rate": 0.5,
                                "ttl_s": 2592000},
                  "answer": {"hits": 0, "misses": 0, "keys": 0, "hit_rate": None,
                             "ttl_s": 3600}}},
    ("POST", "/admin/cache/clear"): {"cleared": {"answer": 4, "search": 6}, "available": True},
    ("GET", "/conversations"): [{"conversation_id": "conv1", "title": "Alpha?",
                                 "created_at": "2026-11-17T10:00:00", "updated_at": None}],
    ("GET", "/conversations/conv1/messages"): [
        {"role": "user", "content": "Alpha?", "trace_id": "t1"},
        {"role": "assistant", "content": "Alpha [1].", "trace_id": "t1", "grounded": None,
         "citations": [{"n": 1, "chunk_id": "c1", "source": "a.md", "heading_path": "",
                        "cited": True}]}],
    ("GET", "/documents"): [{"doc_id": "d1", "source": "a.md", "name": "a.md", "title": "A",
                             "size": 10, "chunk_count": 1, "origin": "docs_dir",
                             "status": "ingested", "ingested_at": "2026-11-17T10:00:00+00:00",
                             "updated_at": None, "embedding_model": "fake"}],
    ("GET", "/documents/d1"): {"doc_id": "d1", "source": "a.md", "name": "a.md",
                               "chunks": [{"chunk_id": "c1", "position": 0, "heading_path": "A",
                                           "text": "alpha", "char_count": 5}]},
    ("POST", "/documents/upload"): {"jobs": [{"job_id": "j1", "status": "done", "results": [
        {"source": "uploads/x.md", "status": "ingested", "chunks_new": 2}], "errors": []}]},
    ("GET", "/ingestion-jobs"): [{"job_id": "j1", "files": ["x.md"], "trigger": "upload",
                                  "status": "done", "results": [], "errors": [],
                                  "duration_s": 0.123, "chunks_created": 2,
                                  "started_at": "2026-11-17T10:00:00"}],
    ("GET", "/logs"): [{"ts": "2026-11-17T10:00:00", "level": "INFO", "service": "api",
                        "message": "http_request", "request_id": "r1", "trace_id": "t1",
                        "extra": {"path": "/ask", "status": 200}, "trace_url": "http://lf/t1"}],
    ("GET", "/eval-runs"): [
        {"run_id": "e1", "name": "baseline", "mode": "hybrid", "created_at": "2026-11-17",
         "metrics": {"faithfulness": 0.8, "answer_relevancy": {"mean": 0.7}, "n": "x"}},
        {"run_id": "e2", "config": {"mode": "bm25"}},  # no metrics at all
        "garbage",
    ],
    ("GET", "/eval-runs/e1"): {"run_id": "e1", "name": "baseline", "mode": "hybrid",
                               "metrics": {"faithfulness": 0.8}, "config": {"top_k": 5},
                               "langfuse_url": "http://lf/datasets/1", "report_path": "r.md",
                               "items": [{"question": "q?", "scores": {"faithfulness": 1.0}},
                                         "odd item"]},
}


@pytest.fixture
def fake_api():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        key = (request.method, request.url.path)
        if key == ("POST", "/ask") and b"fail" in request.content:
            return httpx.Response(503, json={"detail": "GOOGLE_API_KEY is not set"})
        if key not in FAKE:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=FAKE[key])

    old = api_client._client
    api_client._client = api_client.ApiClient(base_url="http://api.test", api_key="",
                                              transport=httpx.MockTransport(handler))
    yield calls
    api_client._client = old


def with_trigger(prop_id, value, fn, *args):
    """Run a callback function with dash.ctx pointing at `prop_id`."""
    def run():
        context_value.set(AttributeDict(triggered_inputs=[{"prop_id": prop_id, "value": value}]))
        return fn(*args)

    return copy_context().run(run)


def dumps(component) -> str:
    return json.dumps(component, cls=plotly.utils.PlotlyJSONEncoder)


# --- structure ---------------------------------------------------------------------------
def test_all_six_pages_registered():
    got = {p["path"]: p["name"] for p in dash.page_registry.values()}
    assert got == EXPECTED_PATHS
    orders = sorted(p["order"] for p in dash.page_registry.values())
    assert orders == [0, 1, 2, 3, 4, 5]


def test_layouts_build_without_api_calls():
    for page in dash.page_registry.values():
        layout = page["layout"]
        tree = layout() if callable(layout) else layout
        assert len(dumps(tree)) > 100
    assert "Hybrid RAG" in dumps(app.layout())


def test_server_routes_and_no_duplicate_outputs():
    client = server.test_client()
    for path in ["/", "/documents", "/retrieval", "/evaluation", "/logs", "/settings",
                 "/_dash-layout", "/healthz"]:
        assert client.get(path).status_code == 200, path
    deps = client.get("/_dash-dependencies").json
    outputs = [d["output"] for d in deps]
    assert len(outputs) == len(set(outputs))
    assert len(deps) >= 20


# --- chat ----------------------------------------------------------------------------------
def test_chat_send_then_ask(fake_api):
    msgs, _cid, text, pending, alert = with_trigger(
        "chat-send.n_clicks", 1, chat.chat_controller, 1, 0, 0, [], " what is alpha ",
        "hybrid", [])
    assert msgs == [{"role": "user", "content": "what is alpha"}]
    assert text == "" and pending["q"] == "what is alpha" and alert is None

    msgs2, cid, _ = chat.chat_ask(pending, msgs, None)
    assert cid == "conv1"
    ans = msgs2[-1]
    assert ans["role"] == "assistant" and ans["grounded"] is True
    assert ans["rewritten_query"] is None  # identical to the question → hidden
    body = json.loads(fake_api[-1].content)
    assert body["source"] == "ui" and body["mode"] == "hybrid"
    html_json = dumps(chat.render_messages(msgs2))
    assert "Open trace in Langfuse" in html_json and "chat-fb-up" in html_json


def test_chat_ask_503_shows_message(fake_api):
    msgs, _, _ = chat.chat_ask({"q": "fail please", "mode": "dense"}, [], None)
    assert msgs[-1]["role"] == "error"
    assert "not ready" in msgs[-1]["content"] and "GOOGLE_API_KEY" in msgs[-1]["content"]


def test_chat_load_conversation_and_new(fake_api):
    out = with_trigger('{"cid":"conv1","type":"chat-conv"}.n_clicks', 1, chat.chat_controller,
                       0, 0, 0, [1], "", "hybrid", [])
    msgs, cid = out[0], out[1]
    assert cid == "conv1" and [m["role"] for m in msgs] == ["user", "assistant"]
    assert "not stored" in dumps(chat.render_messages(msgs))
    new = with_trigger("chat-new.n_clicks", 1, chat.chat_controller, 0, 0, 1, [], "", "hybrid",
                       msgs)
    assert new[0] == [] and new[1] is None
    convs, label = chat.chat_conversations(0, "conv1")
    assert "Alpha?" in dumps(convs) and "conv1" in label


def test_chat_feedback_reports_langfuse_disabled(fake_api):
    msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "trace_id": "t1"}]
    status, up_dis, down_dis = with_trigger('{"idx":1,"type":"chat-fb-up"}.n_clicks', 1,
                                            chat.chat_feedback, 1, 0, msgs)
    assert "disabled" in status and up_dis and down_dis
    assert json.loads(fake_api[-1].content) == {"trace_id": "t1", "value": 1}


# --- other pages ---------------------------------------------------------------------------
def test_retrieval_search(fake_api):
    assert retrieval.retr_defaults(0) == (5, 0.5, 20, 20)
    out = retrieval.retr_search(1, 0, "q", 5, 0.5, 20, 20)
    dense, bm25, fused = out[0], out[1], out[2]
    assert len(dense) == 1 and len(bm25) == 1
    assert [r["found_by"] for r in fused] == ["dense only", "bm25 only"]
    fig = out[7]
    assert {t.name for t in fig.data} == {"dense only", "bm25 only"}
    assert out[9] is None


def test_documents_load_upload_and_detail(fake_api):
    out = with_trigger("docs-init.data", 0, documents.docs_actions, 0, 0, None, 0, 0, 0,
                       None, False, None)
    assert out[0][0]["id"] == "d1" and out[1][0]["chunks_created"] == 2
    payload = "data:text/markdown;base64," + base64.b64encode(b"# X\nhello").decode()
    out = with_trigger("docs-upload.contents", [payload], documents.docs_actions, 0, 0,
                       [payload], 0, 0, 0, ["x.md"], False, None)
    assert "uploads/x.md" in dumps(out[2]) and out[5] is None
    upload_req = next(r for r in fake_api if r.url.path == "/documents/upload")
    assert b"hello" in upload_req.content
    body, title, re_dis, del_dis = documents.docs_detail("d1", [])
    assert title == "a.md" and not re_dis and not del_dis and "alpha" in dumps(body)
    assert documents.docs_detail("missing", [])[1] == "No document selected"


def test_evaluation_is_defensive(fake_api):
    rows, cols, fig, alert = evaluation.eval_load(0, 0)
    assert [r["run_id"] for r in rows] == ["e1", "e2"]
    assert rows[0]["metric_faithfulness"] == 0.8 and rows[0]["metric_answer_relevancy"] == 0.7
    assert rows[1]["mode"] == "bm25"
    assert any(c["id"] == "metric_faithfulness" for c in cols)
    assert len(fig.data) == 1
    body, title = evaluation.eval_detail(["e1"])
    j = dumps(body)
    assert "http://lf/datasets/1" in j and "scores.faithfulness" in j and title.startswith("baseline")


def test_logs_and_settings(fake_api):
    rows, count, alert = logs.logs_load(0, 0, "api", "", "", "")
    assert rows[0]["trace"] == "[open](http://lf/t1)" and "path=/ask" in rows[0]["extra"]
    assert dict(fake_api[-1].url.params) == {"service": "api", "limit": "200"}
    cards, status, config, stats, ui, updated = settings.settings_refresh(0)
    j = dumps(cards)
    assert "HF_TOKEN missing" in j and "DOWN" in j
    assert "degraded" in dumps(status)


# --- cache (Redis) -------------------------------------------------------------------------
def test_chat_cached_badge(fake_api):
    FAKE[("POST", "/ask")]["cached"] = True
    try:
        msgs, _, _ = chat.chat_ask({"q": "what is alpha", "mode": "hybrid"}, [], None)
    finally:
        del FAKE[("POST", "/ask")]["cached"]
    assert msgs[-1]["cached"] is True
    assert '"cached"' in dumps(chat.render_messages(msgs))
    assert '"cached"' not in dumps(chat.render_messages([{"role": "assistant", "content": "x"}]))
    hist = chat.from_api_message({"role": "assistant", "content": "x", "cached": True})
    assert hist["cached"] is True and '"cached"' in dumps(chat.render_messages([hist]))


def test_retrieval_cached_badge(fake_api):
    FAKE[("POST", "/search")]["cached"] = True
    try:
        out = retrieval.retr_search(1, 0, "q", 5, 0.5, 20, 20)
    finally:
        del FAKE[("POST", "/search")]["cached"]
    assert '"cached"' in dumps(out[6])
    assert '"cached"' not in dumps(retrieval.retr_search(1, 0, "q", 5, 0.5, 20, 20)[6])


def test_settings_cache_panel(fake_api):
    j = dumps(with_trigger("settings-interval.n_intervals", 1, settings.settings_cache, 0, None))
    assert "embedding" in j and "50%" in j and "1.2M" in j and "256.00M" in j and "7" in j
    assert "n/a" in j  # hit_rate None
    assert not [r for r in fake_api if r.url.path == "/admin/cache/clear"]
    out = with_trigger("settings-cache-confirm.submit_n_clicks", 1, settings.settings_cache, 0, 1)
    assert "Cache cleared" in dumps(out)
    req = next(r for r in fake_api if r.url.path == "/admin/cache/clear")
    assert dict(req.url.params) == {"kind": "all"}
    assert settings.settings_cache_confirm(1) is True


@pytest.mark.parametrize("payload,text", [
    ({"enabled": False, "available": False}, "disabled"),
    ({"enabled": True, "available": False, "error": "conn refused"}, "not available"),
])
def test_settings_cache_panel_unavailable(fake_api, payload, text):
    old = FAKE[("GET", "/admin/cache/stats")]
    FAKE[("GET", "/admin/cache/stats")] = payload
    try:
        j = dumps(with_trigger("settings-interval.n_intervals", 1, settings.settings_cache, 0, None))
    finally:
        FAKE[("GET", "/admin/cache/stats")] = old
    assert text in j


def test_redis_health_card_renders():
    j = dumps(settings.health_card("redis", {"ok": True, "detail": "PONG"}))
    assert "Redis cache" in j and "PONG" in j
