"""Chat model factory: Gemini (default) or an offline fake model for tests."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from backend_app.config import get_settings


class FakeRAGChatModel(BaseChatModel):
    """Deterministic stand-in for Gemini. Answers by quoting the first context excerpt,
    so the full graph (grading, citations, grounding) runs without an API key."""

    @property
    def _llm_type(self) -> str:
        return "fake-rag"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None,
                  **kwargs: Any) -> ChatResult:
        system = str(messages[0].content) if messages else ""
        user = str(messages[-1].content) if messages else ""
        if system.startswith("TASK: REWRITE"):
            m = re.search(r"(?:Latest question|Question|Query):\s*(.+)", user)
            out = (m.group(1) if m else user).strip()
        elif system.startswith("TASK: GRADE"):
            nums = sorted({int(n) for n in re.findall(r"^\[(\d+)\]", user, re.M)})
            out = json.dumps({"relevant": nums})
        elif system.startswith("TASK: GROUNDING"):
            out = json.dumps({"grounded": True, "reason": "fake model always agrees"})
        else:
            m = re.search(r"^\[1\][^\n]*\n(.+?)(?:\n\[2\]|\Z)", user, re.S | re.M)
            if m:
                first = " ".join(m.group(1).split())[:300]
                out = f"According to the documents: {first} [1]"
            else:
                out = "I couldn't find this in the documents."
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=out))])


@lru_cache
def get_chat_model() -> BaseChatModel:
    s = get_settings()
    if s.llm_provider == "fake":
        return FakeRAGChatModel()
    if not s.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set (or use LLM_PROVIDER=fake for testing)")
    from langchain_google_genai import ChatGoogleGenerativeAI

    kwargs: dict[str, Any] = {
        "model": s.llm_model,
        "google_api_key": s.google_api_key,
        "temperature": s.llm_temperature,
        "max_retries": 3,
        "timeout": 60,
    }
    if s.llm_thinking_budget >= 0:
        kwargs["thinking_budget"] = s.llm_thinking_budget
    return ChatGoogleGenerativeAI(**kwargs)


def text_of(message: Any) -> str:
    """Gemini may return content as a list of parts; flatten to plain text."""
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content)


def parse_json(text: str) -> dict:
    """Parse a JSON object from an LLM reply that may be wrapped in ```json fences."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON object in: {text[:200]}")
    return json.loads(m.group(0))


def check_llm() -> tuple[bool, str]:
    """No API call (saves free-tier quota): only checks the provider is configured."""
    s = get_settings()
    if s.llm_provider == "fake":
        return True, "fake model (offline)"
    if not s.google_api_key:
        return False, "GOOGLE_API_KEY not set"
    return True, f"configured ({s.llm_model})"
