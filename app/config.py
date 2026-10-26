from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "hy-memory-aml"
    app_version: str = "0.1.0"
    environment: str = "development"
    api_token: str = "change-me"
    data_dir: Path = Path("./data/adapter")
    adapter_db_path: Path | None = None
    memory_mode: str = "pro"
    max_add_concurrency: int = Field(default=4, ge=1, le=64)
    max_search_concurrency: int = Field(default=8, ge=1, le=128)
    raw_evidence_limit: int = Field(default=20, ge=0, le=100)
    include_options_in_query: bool = False
    docs_enabled: bool = True
    log_level: str = "INFO"
    fast_memory_enabled: bool = False
    # Read previously indexed evidence after restoring synchronous Hy-Memory.
    # This does not skip LLM extraction on any new write.
    fast_memory_read_fallback: bool = False
    fast_memory_collection_name: str = "aml_raw_fast_v1"
    fast_memory_persist_dir: Path = Path("./data/fast-chroma")
    fast_memory_max_document_chars: int = Field(default=16_000, ge=1_000, le=100_000)
    fast_memory_backfill_hours: float = Field(default=0.0, ge=0.0, le=168.0)
    memory_embedder_model: str = "text-embedding-v4"
    memory_embedder_api_key: str = ""
    memory_embedder_base_url: str = "https://api.openai.com/v1"
    memory_embedding_dims: int = Field(default=1024, ge=1, le=8192)

    @field_validator("api_token")
    @classmethod
    def validate_api_token(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("API_TOKEN must not be empty")
        return value

    @model_validator(mode="after")
    def reject_default_production_token(self) -> Settings:
        if self.is_production and self.api_token == "change-me":
            raise ValueError("API_TOKEN must be changed in production")
        return self

    @property
    def db_path(self) -> Path:
        return self.adapter_db_path or self.data_dir / "adapter.sqlite3"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()

