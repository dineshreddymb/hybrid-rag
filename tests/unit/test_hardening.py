"""Hardening features stay deterministic without running containers."""
import fakeredis
import redis as redis_lib

from backend_app import backups
from backend_app.cache import redis_client
from backend_app.config import get_settings
from backend_app.security import rate_limit


def test_rate_limit_has_separate_ask_budget_and_resets_each_window(monkeypatch):
    fake = fakeredis.FakeRedis()
    redis_client.reset(fake)
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_enabled", True)
    monkeypatch.setattr(s, "rate_limit_requests_per_minute", 3)
    monkeypatch.setattr(s, "rate_limit_ask_per_minute", 2)
    try:
        assert rate_limit.check("/ask", client_host="127.0.0.1", now=120.0).allowed
        assert rate_limit.check("/ask", client_host="127.0.0.1", now=120.0).allowed
        blocked = rate_limit.check("/ask", client_host="127.0.0.1", now=120.0)
        assert not blocked.allowed and blocked.retry_after_s == 60
        # A different endpoint gets its own, larger budget.
        assert rate_limit.check("/search", client_host="127.0.0.1", now=120.0).allowed
        # A new minute starts a fresh window.
        assert rate_limit.check("/ask", client_host="127.0.0.1", now=180.0).allowed
    finally:
        redis_client.reset(None)


def test_rate_limit_fails_open_when_redis_is_down(monkeypatch):
    class Broken:
        def incr(self, *_args):
            raise redis_lib.ConnectionError("down")

    redis_client.reset(Broken())
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    try:
        assert rate_limit.check("/ask", client_host="127.0.0.1").allowed
    finally:
        redis_client.reset(None)


def test_scheduled_snapshot_prunes_only_after_a_success(monkeypatch):
    deleted = []
    monkeypatch.setattr(get_settings(), "qdrant_snapshot_keep", 2)
    monkeypatch.setattr(backups.qdrant_store, "create_snapshot", lambda: {"name": "new", "size": 1})
    monkeypatch.setattr(backups.qdrant_store, "list_snapshots", lambda: [
        {"name": "new"}, {"name": "middle"}, {"name": "old"},
    ])
    monkeypatch.setattr(backups.qdrant_store, "delete_snapshot", deleted.append)
    result = backups.snapshot_once()
    assert result["last_status"] == "ok" and result["last_snapshot"] == "new"
    assert deleted == ["old"]


def test_public_config_reports_hardening_settings():
    summary = get_settings().public_summary()
    assert "rate_limit_enabled" in summary
    assert "qdrant_snapshot_interval_hours" in summary
