"""MCP server logging: stdio mode must never write to stdout (it would corrupt the protocol)."""
from __future__ import annotations

import io
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp-server"))

from mcp_server import logging_setup  # noqa: E402


def _with_streams(transport: str) -> tuple[str, str]:
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        logging_setup.setup_logging(transport, level="INFO", ship_logs=False)
        token = logging_setup.request_id_var.set("rid-1")
        logging_setup.get_logger("mcp_server.tools").info(
            "tool_call", extra={"tool": "ask", "ok": True, "ms": 1.5, "trace_id": "t1"})
        logging_setup.request_id_var.reset(token)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])
    return out.getvalue(), err.getvalue()


def test_stdio_logs_go_to_stderr_as_json():
    out, err = _with_streams("stdio")
    assert out == ""
    line = json.loads(err.strip().splitlines()[-1])
    assert line["service"] == "mcp" and line["message"] == "tool_call"
    assert line["level"] == "INFO" and line["request_id"] == "rid-1" and line["trace_id"] == "t1"
    assert line["tool"] == "ask" and line["ok"] is True and line["ms"] == 1.5


def test_http_logs_go_to_stdout():
    out, err = _with_streams("http")
    assert err == ""
    assert json.loads(out.strip())["message"] == "tool_call"


def _record(msg="m"):
    return logging.LogRecord("mcp_server", logging.INFO, __file__, 1, msg, None, None)


def test_handler_never_blocks_when_queue_full(monkeypatch):
    import queue

    monkeypatch.setattr(logging_setup, "_log_queue", queue.Queue(maxsize=1))
    handler = logging_setup.HttpQueueHandler()
    handler.emit(_record())
    handler.emit(_record())  # queue full: dropped silently
    doc = logging_setup._log_queue.get_nowait()
    assert logging_setup._log_queue.empty()
    assert doc["level"] == "INFO" and doc["message"] == "m"
    assert set(doc) == {"ts", "level", "message", "request_id", "trace_id", "extra"}
    assert isinstance(doc["ts"], str) and datetime.fromisoformat(doc["ts"])


def test_shipper_posts_one_batch_to_backend(monkeypatch):
    import queue
    import threading

    import httpx

    calls: list[httpx.Request] = []
    done = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        done.set()
        return httpx.Response(200, json={"stored": 3})

    monkeypatch.setattr(logging_setup, "_log_queue", queue.Queue(maxsize=100))
    monkeypatch.setattr(logging_setup, "_make_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(logging_setup, "FLUSH_SECONDS", 0.2)
    monkeypatch.setenv("BACKEND_URL", "http://backend.test:8000")
    monkeypatch.setenv("API_KEY", "secret")
    h = logging_setup.HttpQueueHandler()
    for i in range(3):
        h.emit(_record(f"m{i}"))
    threading.Thread(target=logging_setup._shipper, daemon=True).start()
    assert done.wait(5)
    assert len(calls) == 1
    req = calls[0]
    assert str(req.url) == "http://backend.test:8000/logs/batch"
    assert req.headers["x-api-key"] == "secret"
    body = json.loads(req.content)
    assert body["service"] == "mcp"
    assert [e["message"] for e in body["entries"]] == ["m0", "m1", "m2"]


def test_shipper_omits_api_key_and_caps_batch(monkeypatch):
    import httpx

    seen = []
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("BACKEND_URL", raising=False)
    monkeypatch.setenv("API_URL", "http://legacy:8000")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: seen.append(r) or httpx.Response(200)))
    logging_setup._ship_batch(client, [{"message": "x"}])
    assert "x-api-key" not in seen[0].headers
    assert str(seen[0].url) == "http://legacy:8000/logs/batch"
    assert logging_setup.BATCH_MAX == 100


def test_shipper_survives_unreachable_backend(monkeypatch):
    import queue
    import threading
    import time

    import httpx

    attempts = []

    def boom(request):
        attempts.append(1)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(logging_setup, "_log_queue", queue.Queue(maxsize=100))
    monkeypatch.setattr(logging_setup, "_make_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(boom)))
    monkeypatch.setattr(logging_setup, "FLUSH_SECONDS", 0.05)
    h = logging_setup.HttpQueueHandler()
    t = threading.Thread(target=logging_setup._shipper, daemon=True)
    t.start()
    h.emit(_record("a"))
    time.sleep(0.5)
    h.emit(_record("b"))  # still accepted after a failure
    time.sleep(0.5)
    assert t.is_alive() and len(attempts) >= 2
