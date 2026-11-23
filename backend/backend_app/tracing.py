"""Langfuse tracing (Python SDK v5). Fail-open: if Langfuse is not configured or is down,
everything still works and these helpers become no-ops.

Usage:
    with trace_request("ask", input={...}, session_id=conv_id, tags=["source:ui"]) as t:
        handler = t.callback_handler()        # pass to LangGraph / LangChain config
        ...
        t.update(output={...})
    t.trace_id  # available inside and after the block
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Iterator

from backend_app.config import get_settings
from backend_app.logging import get_logger, trace_id_var

log = get_logger(__name__)
_client = None
_client_failed = False


def get_langfuse():
    """Return a Langfuse client, or None when tracing is disabled/unavailable."""
    global _client, _client_failed
    s = get_settings()
    if not s.langfuse_enabled or _client_failed:
        return None
    if _client is None:
        try:
            from langfuse import Langfuse

            _client = Langfuse(
                public_key=s.langfuse_public_key.strip("\"'"),
                secret_key=s.langfuse_secret_key.strip("\"'"),
                base_url=s.langfuse_server_url,
                sample_rate=s.langfuse_sample_rate,
                environment="production" if "cloud" in s.langfuse_server_url else "local",
            )
        except Exception as exc:  # noqa: BLE001
            _client_failed = True
            log.warning("langfuse_init_failed", error=str(exc))
            return None
    return _client


def trace_url(trace_id: str | None) -> str | None:
    s = get_settings()
    if not trace_id or not s.langfuse_enabled:
        return None
    lf = get_langfuse()
    if lf is not None:
        try:
            url = lf.get_trace_url(trace_id=trace_id)
            if url:
                return url
        except Exception:
            pass
    base = s.langfuse_effective_public_url.rstrip("/")
    if s.langfuse_project_id:
        return f"{base}/project/{s.langfuse_project_id}/traces/{trace_id}"
    return f"{base}/trace/{trace_id}"


@dataclass
class TraceHandle:
    name: str
    trace_id: str | None = None
    _span: Any = None
    _extra_output: dict = field(default_factory=dict)

    def callback_handler(self):
        """LangChain/LangGraph callback that nests every node/LLM call under this trace."""
        if self._span is None:
            return None
        try:
            from langfuse.langchain import CallbackHandler

            return CallbackHandler()
        except Exception as exc:  # noqa: BLE001
            log.warning("langfuse_callback_failed", error=str(exc))
            return None

    def update(self, **kwargs) -> None:
        if self._span is not None:
            try:
                self._span.update(**kwargs)
            except Exception:  # noqa: BLE001
                pass

    @contextlib.contextmanager
    def span(self, name: str, input: Any = None, as_type: str = "span") -> Iterator[Any]:
        """Child span for non-LangChain code (e.g. ingestion steps)."""
        lf = get_langfuse()
        if lf is None or self._span is None:
            yield None
            return
        try:
            cm = lf.start_as_current_observation(name=name, input=input, as_type=as_type)
        except Exception:  # noqa: BLE001
            yield None
            return
        with cm as child:
            yield child


@contextlib.contextmanager
def trace_request(name: str, input: Any = None, session_id: str | None = None,
                  user_id: str = "local", tags: list[str] | None = None,
                  metadata: dict | None = None) -> Iterator[TraceHandle]:
    handle = TraceHandle(name=name)
    lf = get_langfuse()
    if lf is None:
        yield handle
        return

    try:
        from langfuse import propagate_attributes

        attrs = propagate_attributes(
            trace_name=name,
            session_id=session_id,
            user_id=user_id,
            tags=tags or [],
            metadata={k: str(v) for k, v in (metadata or {}).items()},
        )
        span_cm = lf.start_as_current_observation(name=name, input=input, as_type="chain")
    except Exception as exc:  # noqa: BLE001
        log.warning("langfuse_trace_start_failed", error=str(exc))
        yield handle
        return

    with span_cm as span, attrs:
        handle._span = span
        try:
            handle.trace_id = lf.get_current_trace_id()
        except Exception:  # noqa: BLE001
            handle.trace_id = None
        token = trace_id_var.set(handle.trace_id)
        try:
            yield handle
        finally:
            trace_id_var.reset(token)


def score(trace_id: str | None, name: str, value: float | str, comment: str | None = None,
          data_type: str | None = None) -> bool:
    lf = get_langfuse()
    if lf is None or not trace_id:
        return False
    try:
        lf.create_score(trace_id=trace_id, name=name, value=value, comment=comment,
                        data_type=data_type)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("langfuse_score_failed", error=str(exc))
        return False


def check_langfuse() -> tuple[bool, str]:
    s = get_settings()
    if not s.langfuse_enabled:
        return False, "disabled (no LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)"
    lf = get_langfuse()
    if lf is None:
        return False, "client init failed"
    try:
        return (True, "ok") if lf.auth_check() else (False, "auth failed")
    except Exception as exc:  # noqa: BLE001
        return False, f"unreachable: {exc.__class__.__name__}"


def flush() -> None:
    lf = get_langfuse()
    if lf is not None:
        try:
            lf.flush()
        except Exception:  # noqa: BLE001
            pass
