"""Single source of truth for configuration.

Everything is read from the environment (12-factor). Nothing in the codebase
reads os.environ directly - it goes through `settings` so that tests can
override cleanly and so that a missing variable fails at boot, not at 3am.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---------------- app ----------------
    app_name: str = "itsm-agentic-platform"
    app_env: Literal["local", "dev", "staging", "prod"] = "local"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    request_timeout_seconds: int = 60

    # ---------------- auth ----------------
    jwt_secret: str = "change-me-in-prod"
    jwt_algorithm: str = "HS256"
    service_api_keys: list[str] = Field(default_factory=lambda: ["local-dev-key"])

    # ---------------- postgres ----------------
    postgres_dsn: str = "postgresql+asyncpg://itsm:itsm@localhost:5432/itsm"
    postgres_sync_dsn: str = "postgresql://itsm:itsm@localhost:5432/itsm"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_echo: bool = False

    # ---------------- redis ----------------
    redis_url: str = "redis://localhost:6379/0"
    redis_namespace: str = "itsm"
    rate_limit_per_minute: int = 60
    cache_ttl_seconds: int = 900

    # ---------------- opensearch ----------------
    opensearch_hosts: list[str] = Field(default_factory=lambda: ["http://localhost:9200"])
    opensearch_user: str = "admin"
    opensearch_password: str = "admin"
    opensearch_index: str = "itsm-knowledge-v1"
    opensearch_verify_certs: bool = False

    # ---------------- s3 ----------------
    s3_bucket: str = "itsm-ingestion"
    s3_prefix: str = "raw/"
    aws_region: str = "us-east-1"
    aws_endpoint_url: str | None = None

    # ---------------- model access ----------------
    primary_model: str = "openai/gpt-4o-mini"
    fallback_models: list[str] = Field(default_factory=list)
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dim: int = 1536
    rerank_model: str = "openai/gpt-4o-mini"
    llm_timeout_seconds: int = 45
    llm_max_retries: int = 2
    daily_budget_usd: float = 250.0

    # ---------------- observability ----------------
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "itsm-agentic-platform"
    metrics_enabled: bool = True

    # ---------------- workers ----------------
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    ingestion_chunk_size: int = 900
    ingestion_chunk_overlap: int = 150
    ingestion_max_file_mb: int = 50

    # ---------------- agent behaviour ----------------
    retrieval_top_k: int = 30
    rerank_top_n: int = 6
    min_confidence_to_auto_resolve: float = 0.72
    max_agent_steps: int = 12

    @field_validator("postgres_dsn")
    @classmethod
    def _validate_async_dsn(cls, v: str) -> str:
        if "+asyncpg" not in v:
            raise ValueError("postgres_dsn must use the asyncpg driver")
        return v

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

__all__ = ["Settings", "get_settings", "settings", "PostgresDsn"]
