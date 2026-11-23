"""Unit tests for the Dash UI's API client (no network: httpx.MockTransport)."""
import json
import sys
from pathlib import Path

import httpx
import pytest

UI_DIR = Path(__file__).resolve().parents[2] / "frontend"
if str(UI_DIR) not in sys.path:
    sys.path.insert(0, str(UI_DIR))

from frontend_app.api_client import ApiClient, ApiError  # noqa: E402


def make_client(handler, api_key=""):
    seen = []

    def wrapped(request: httpx.Request):
        seen.append(request)
        return handler(request)

    client = ApiClient(base_url="http://api.test", api_key=api_key,
                       transport=httpx.MockTransport(wrapped))
    return client, seen


def test_ask_sends_body_and_headers():
    answer = {"answer": "42", "citations": [], "conversation_id": "c1", "trace_id": "t1"}
    client, seen = make_client(lambda r: httpx.Response(200, json=answer), api_key="secret")
    out = client.ask("What?", conversation_id="c1", mode="bm25")
    assert out == answer
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/ask"
    assert req.headers["x-api-key"] == "secret"
    assert len(req.headers["x-request-id"]) == 16
    body = json.loads(req.content)
    assert body == {"question": "What?", "conversation_id": "c1", "mode": "bm25",
                    "source": "ui", "use_history": True}


def test_no_api_key_header_when_unset():
    client, seen = make_client(lambda r: httpx.Response(200, json={"status": "ok"}))
    assert client.health() == {"status": "ok"}
    assert "x-api-key" not in seen[0].headers


def test_503_becomes_api_error_with_detail():
    client, _ = make_client(lambda r: httpx.Response(503, json={"detail": "GOOGLE_API_KEY missing"}))
    with pytest.raises(ApiError) as ei:
        client.ask("hi")
    assert ei.value.status == 503
    assert ei.value.detail == "GOOGLE_API_KEY missing"
    assert "503" in str(ei.value)


def test_validation_error_list_is_flattened():
    detail = [{"loc": ["body", "question"], "msg": "String should have at least 1 character"}]
    client, _ = make_client(lambda r: httpx.Response(422, json={"detail": detail}))
    with pytest.raises(ApiError) as ei:
        client.search("x")
    assert ei.value.detail == "question: String should have at least 1 character"


def test_connection_error_is_api_error():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    client, _ = make_client(boom)
    with pytest.raises(ApiError) as ei:
        client.documents()
    assert ei.value.status is None
    assert "Cannot reach the API" in str(ei.value)


def test_search_omits_none_fields():
    client, seen = make_client(lambda r: httpx.Response(200, json={"fused": []}))
    client.search("q", top_k=5, weight_dense=0.3)
    body = json.loads(seen[0].content)
    assert body == {"query": "q", "mode": "hybrid", "top_k": 5, "weight_dense": 0.3,
                    "source": "ui"}


def test_upload_uses_multipart_files_field():
    client, seen = make_client(lambda r: httpx.Response(200, json={"jobs": []}))
    client.upload([("a.md", b"# A"), ("b.txt", b"bee")])
    req = seen[0]
    assert req.url.path == "/documents/upload"
    assert req.headers["content-type"].startswith("multipart/form-data")
    raw = req.content
    assert raw.count(b'name="files"') == 2
    assert b'filename="a.md"' in raw and b'filename="b.txt"' in raw


def test_logs_drops_empty_params():
    client, seen = make_client(lambda r: httpx.Response(200, json=[]))
    assert client.logs(service="api", level="", request_id=None, q="boom") == []
    params = dict(seen[0].url.params)
    assert params == {"service": "api", "q": "boom", "limit": "200"}


def test_feedback_and_document_paths():
    client, seen = make_client(lambda r: httpx.Response(200, json={"recorded": False}))
    assert client.feedback("t1", 1) == {"recorded": False}
    assert json.loads(seen[0].content) == {"trace_id": "t1", "value": 1}
    client.delete_document("d1")
    client.reingest("d1")
    client.scan(force=True)
    client.messages("c9")
    client.eval_run("r1")
    got = [(r.method, r.url.path, dict(r.url.params)) for r in seen[1:]]
    assert got == [("DELETE", "/documents/d1", {}), ("POST", "/documents/d1/reingest", {}),
                   ("POST", "/ingest/scan", {"force": "true"}),
                   ("GET", "/conversations/c9/messages", {}), ("GET", "/eval-runs/r1", {})]


def test_empty_body_returns_none():
    client, _ = make_client(lambda r: httpx.Response(204))
    assert client.request("POST", "/anything") is None


def test_cache_stats_and_clear_cache():
    stats = {"enabled": True, "available": True, "kinds": {}}
    client, seen = make_client(lambda r: httpx.Response(200, json=stats if r.method == "GET"
                                                        else {"cleared": "answer", "deleted": 2}))
    assert client.cache_stats() == stats
    assert seen[0].method == "GET" and seen[0].url.path == "/admin/cache/stats"
    assert client.clear_cache("answer")["deleted"] == 2
    assert seen[1].method == "POST" and seen[1].url.path == "/admin/cache/clear"
    assert dict(seen[1].url.params) == {"kind": "answer"}
    client.clear_cache()
    assert dict(seen[2].url.params) == {"kind": "all"}


def test_log_shipper_batches_to_backend():
    from frontend_app.logging_setup import HttpQueueHandler, LogShipper
    import logging

    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"stored": 3})

    shipper = LogShipper(base_url="http://api.test", api_key="k", start=False,
                         transport=httpx.MockTransport(handler))
    h = HttpQueueHandler(shipper)
    for i in range(3):
        h.emit(logging.makeLogRecord({"msg": f"m{i}", "levelno": 20, "levelname": "INFO",
                                      "request_id": "r1", "path": "/x"}))
    assert shipper.flush_once() is True
    assert len(seen) == 1 and seen[0].url.path == "/logs/batch"
    assert seen[0].headers["x-api-key"] == "k"
    body = json.loads(seen[0].content)
    assert body["service"] == "ui" and len(body["entries"]) == 3
    e = body["entries"][0]
    assert e["message"] == "m0" and e["level"] == "INFO" and e["request_id"] == "r1"
    assert e["trace_id"] is None and isinstance(e["ts"], str) and e["extra"]["path"] == "/x"
    assert shipper.flush_once() is True and len(seen) == 1  # empty queue: no request


def test_log_shipper_never_raises_when_backend_down():
    from frontend_app.logging_setup import HttpQueueHandler, LogShipper
    import logging

    def boom(request):
        raise httpx.ConnectError("down")

    shipper = LogShipper(base_url="http://api.test", api_key="", start=False, maxsize=2,
                         transport=httpx.MockTransport(boom))
    h = HttpQueueHandler(shipper)
    for i in range(5):  # overflow is dropped silently
        h.emit(logging.makeLogRecord({"msg": "m"}))
    assert shipper.flush_once() is False
