"""All settings come from environment variables (see .env.example).

pydantic-settings reads them once; every module uses `get_settings()`.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    service_name: str = "api"

    # --- LLM -------------------------------------------------------------
    llm_provider: Literal["gemini", "fake"] = "gemini"
    google_api_key: str = ""
    llm_model: str = "gemini-3.1-flash-lite"
    llm_temperature: float = 0.0
    # Gemini 2.5 Flash "thinking" tokens: 0 = off (cheaper, faster). -1 = don't send the
    # parameter at all (needed for models that cannot turn thinking off).
    llm_thinking_budget: int = 0

    # --- Embeddings --------------------------------------------------------
    embedding_provider: Literal["hf_api", "tei", "fake", "jina"] = "jina"
    hf_token: str = ""
    jina_api_key: str = ""
    embedding_model: str = "jina-embeddings-v3"
    embedding_dim: int = 1024
    embedding_batch_size: int = 32
    hf_api_url: str = (
        "https://router.huggingface.co/hf-inference/models/{model}/pipeline/feature-extraction"
    )
    jina_embedding_url: str = "https://api.jina.ai/v1/embeddings"
    tei_url: str = "http://embeddings:80"
    # bge models expect this prefix on queries (not on documents).
    embedding_query_prefix: str = "Represent this sentence for searching relevant passages: "

    # --- Reranker (Jina AI) ------------------------------------------------
    rerank_provider: Literal["none", "jina"] = "none"
    rerank_model: str = "jina-reranker-v2-base-multilingual"
    jina_rerank_url: str = "https://api.jina.ai/v1/rerank"

    # --- Chunking / retrieval ---------------------------------------------
    chunk_size: int = 800
    chunk_overlap: int = 120
    top_k_dense: int = 10
    top_k_bm25: int = 10
    top_k_final: int = 3
    hybrid_weight_dense: float = Field(0.5, ge=0.0, le=1.0)
    rrf_k: int = 60

    # --- RAG graph ---------------------------------------------------------
    fast_mode: bool = True
    enable_grading: bool = False
    enable_grounding_check: bool = False
    max_rewrites: int = 1
    history_messages: int = 6

    # --- Stores ------------------------------------------------------------
    mongo_uri: str = "mongodb://mongodb:27017"
    mongo_db: str = "hybrid_rag"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "chunks_jina_v3"

    # --- Files -------------------------------------------------------------
    docs_dir: str = "/data/docs"
    auto_ingest_on_startup: bool = True
    docs_remote_zip_url: str = ""
    docs_sync_interval_seconds: int = 300
    docs_sync_enabled: bool = True
    supabase_url: str = "https://eedxymqeghvxjkzfvvbd.supabase.co"
    supabase_anon_key: str = ""
    supabase_bucket: str = "docs"

    # --- Redis cache (backend only) --------------------------------------------
    cache_enabled: bool = True
    redis_password: str = ""
    # ${REDIS_PASSWORD} is replaced by redis_password (env files do not expand variables)
    redis_url: str = "redis://:${REDIS_PASSWORD}@redis:6379/0"
    cache_ttl_answer_s: int = 86400
    cache_ttl_search_s: int = 600
    cache_ttl_embedding_s: int = 2592000

    # --- Langfuse ----------------------------------------------------------
    langfuse_host: str = "http://langfuse-web:3000"
    langfuse_base_url: str = ""
    langfuse_public_url: str = ""  # used for links shown in the browser
    langfuse_project_id: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_sample_rate: float = 1.0

    # --- Security / logging -------------------------------------------------
    api_key: str = ""  # if set, every request (except /health) needs header X-API-Key
    # Per-client, fixed-window limits. Redis makes the counter shared if the API is scaled out;
    # the limiter deliberately fails open when Redis is unavailable, just like the cache.
    rate_limit_enabled: bool = True
    rate_limit_requests_per_minute: int = Field(120, ge=1, le=10_000)
    rate_limit_ask_per_minute: int = Field(20, ge=1, le=10_000)
    # Automatic Qdrant snapshots are local backups in ./data/qdrant_snapshots.  MongoDB is
    # persisted separately in its named volume and should be backed up by the host task.
    qdrant_snapshot_schedule_enabled: bool = True
    qdrant_snapshot_interval_hours: int = Field(24, ge=1, le=720)
    qdrant_snapshot_keep: int = Field(7, ge=1, le=100)

    # --- Version & Environment metadata ------------------------------------
    app_version: str = "0.1.0"
    git_commit: str = "dev"
    build_date: str = "local"
    environment: str = "production"
    log_level: str = "INFO"
    log_ttl_days: int = 30
    log_to_mongo: bool = True

    @property
    def redis_url_resolved(self) -> str:
        return self.redis_url.replace("${REDIS_PASSWORD}", self.redis_password)

    @property
    def langfuse_server_url(self) -> str:
        url = self.langfuse_base_url or self.langfuse_host
        return url.strip("\"'")

    @property
    def langfuse_effective_public_url(self) -> str:
        url = self.langfuse_public_url
        if url and url != "http://localhost:3000":
            return url.strip("\"'")
        return (self.langfuse_base_url or self.langfuse_public_url or self.langfuse_host).strip("\"'")

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    def public_summary(self) -> dict:
        """Config that is safe to show in the UI (no secrets)."""
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "top_k_dense": self.top_k_dense,
            "top_k_bm25": self.top_k_bm25,
            "top_k_final": self.top_k_final,
            "hybrid_weight_dense": self.hybrid_weight_dense,
            "rrf_k": self.rrf_k,
            "enable_grading": self.enable_grading,
            "enable_grounding_check": self.enable_grounding_check,
            "qdrant_collection": self.qdrant_collection,
            "cache_enabled": self.cache_enabled,
            "cache_ttl_answer_s": self.cache_ttl_answer_s,
            "cache_ttl_search_s": self.cache_ttl_search_s,
            "cache_ttl_embedding_s": self.cache_ttl_embedding_s,
            "rate_limit_enabled": self.rate_limit_enabled,
            "rate_limit_requests_per_minute": self.rate_limit_requests_per_minute,
            "rate_limit_ask_per_minute": self.rate_limit_ask_per_minute,
            "qdrant_snapshot_schedule_enabled": self.qdrant_snapshot_schedule_enabled,
            "qdrant_snapshot_interval_hours": self.qdrant_snapshot_interval_hours,
            "qdrant_snapshot_keep": self.qdrant_snapshot_keep,
            "langfuse_enabled": self.langfuse_enabled,
            "langfuse_public_url": self.langfuse_effective_public_url,
            "hf_token_set": bool(self.hf_token),
            "jina_api_key_set": bool(self.jina_api_key),
            "google_api_key_set": bool(self.google_api_key),
            "rerank_provider": self.rerank_provider,
            "rerank_model": self.rerank_model,
            "docs_remote_zip_url": self.docs_remote_zip_url,
            "docs_sync_enabled": self.docs_sync_enabled,
            "docs_sync_interval_seconds": self.docs_sync_interval_seconds,
            "supabase_url": self.supabase_url,
            "supabase_bucket": self.supabase_bucket,
            "supabase_anon_key_set": bool(self.supabase_anon_key),
            "app_version": self.app_version,
            "git_commit": self.git_commit,
            "build_date": self.build_date,
            "environment": self.environment,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
