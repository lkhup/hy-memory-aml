from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TrackSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
    aml_track: Literal["coding", "multimodal"] = "coding"
    api_token: str = "change-me"
    track_root: Path = Path("./data/coding")
    max_add_concurrency: int = Field(64, ge=1, le=256)
    max_search_concurrency: int = Field(64, ge=1, le=256)
    max_pending_requests: int = Field(256, ge=1, le=2048)
    queue_timeout: float = Field(240, ge=1, le=600)
    chunk_chars: int = Field(4000, ge=500, le=8000)
    memory_mode: str = "pro"
    memory_llm_model: str = "gpt-4o-mini"
    memory_llm_api_key: str = ""
    memory_llm_base_url: str = "https://api.openai.com/v1"
    memory_embedder_model: str = "text-embedding-v4"
    memory_embedder_api_key: str = ""
    memory_embedder_base_url: str = "https://api.openai.com/v1"
    memory_embedding_dims: int = 1024
    vision_enabled: bool = True
    shared_budget_path: Path = Path("./data/shared/provider-budget.sqlite3")
    provider_max_concurrency: int = Field(48, ge=1, le=256)
    provider_initial_concurrency: int = Field(32, ge=1, le=256)
    embedding_max_concurrency: int = Field(64, ge=1, le=256)
    provider_attempts: int = Field(4, ge=1, le=8)

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.api_token == "change-me" or not self.api_token.strip():
            raise ValueError("Set a dedicated API_TOKEN")
        if self.provider_initial_concurrency > self.provider_max_concurrency:
            raise ValueError("initial provider concurrency exceeds maximum")
        return self
