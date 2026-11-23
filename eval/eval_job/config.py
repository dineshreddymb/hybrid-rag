"""Settings for the eval job, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class EvalSettings:
    api_url: str = "http://localhost:8000"
    api_key: str = ""
    eval_dir: Path = Path("tests/eval")
    # judge LLM
    llm_provider: str = "gemini"
    google_api_key: str = ""
    eval_llm_model: str = "gemini-2.5-flash"
    eval_max_workers: int = 2
    eval_timeout_s: int = 180
    # embeddings (for answer relevancy)
    embedding_provider: str = "jina"
    hf_token: str = ""
    jina_api_key: str = ""
    embedding_model: str = "jina-embeddings-v3"
    embedding_dim: int = 1024
    embedding_batch_size: int = 32
    hf_api_url: str = (
        "https://router.huggingface.co/hf-inference/models/{model}/pipeline/feature-extraction"
    )
    jina_embedding_url: str = "https://api.jina.ai/v1/embeddings"
    # Langfuse
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_public_url: str = "http://localhost:3000"
    langfuse_project_id: str = ""
    langfuse_dataset: str = "rag-golden"
    # misc
    ask_timeout_s: float = 180.0
    pause_s: float = 1.0

    @property
    def judge_available(self) -> bool:
        return self.llm_provider != "fake" and bool(self.google_api_key)

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @classmethod
    def from_env(cls) -> "EvalSettings":
        return cls(
            api_url=(_env("BACKEND_URL") or _env("API_URL", "http://localhost:8000")).rstrip("/"),
            api_key=_env("API_KEY"),
            eval_dir=Path(_env("EVAL_DIR", "tests/eval")),
            llm_provider=_env("LLM_PROVIDER", "gemini").lower(),
            google_api_key=_env("GOOGLE_API_KEY"),
            eval_llm_model=_env("EVAL_LLM_MODEL", "gemini-2.5-flash"),
            eval_max_workers=max(1, _env_int("EVAL_MAX_WORKERS", 2)),
            eval_timeout_s=_env_int("EVAL_TIMEOUT_S", 180),
            embedding_provider=_env("EMBEDDING_PROVIDER", "jina").lower(),
            hf_token=_env("HF_TOKEN"),
            jina_api_key=_env("JINA_API_KEY"),
            embedding_model=_env("EMBEDDING_MODEL", "jina-embeddings-v3"),
            embedding_dim=_env_int("EMBEDDING_DIM", 1024),
            embedding_batch_size=_env_int("EMBEDDING_BATCH_SIZE", 32),
            langfuse_host=_env("LANGFUSE_BASE_URL", _env("LANGFUSE_HOST", "http://localhost:3000")),
            langfuse_public_key=_env("LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=_env("LANGFUSE_SECRET_KEY"),
            langfuse_public_url=_env("LANGFUSE_BASE_URL", _env("LANGFUSE_PUBLIC_URL", "http://localhost:3000")).rstrip("/"),
            langfuse_project_id=_env("LANGFUSE_PROJECT_ID", "cmwm4rqch017oad0e0yj7guaz"),
            langfuse_dataset=_env("LANGFUSE_DATASET", "rag-golden"),
            ask_timeout_s=float(_env("EVAL_ASK_TIMEOUT_S", "180") or 180),
            pause_s=float(_env("EVAL_PAUSE_S", "1.0") or 1.0),
        )
