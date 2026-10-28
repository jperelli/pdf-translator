from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every field can be set with an env var of the same name (upper case)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str | None = None
    # "openai" uses the real models; "pseudo" runs the same agents against a local fake model
    # (pseudo-localization, zero tokens) for development, tests and demos without a key.
    # "auto" picks openai when OPENAI_API_KEY is set.
    llm_backend: Literal["auto", "openai", "pseudo"] = "auto"

    # Cheap model for the bulk of the work, stronger models where quality is decided.
    analyst_model: str = "gpt-5.4-mini"
    translator_model: str = "gpt-5.4-mini"
    judge_model: str = "gpt-5.4"
    escalation_model: str = "gpt-5.5"
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] = "none"
    judge_reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] = "low"

    # Quality loop
    quality_threshold: float = 85.0
    max_refine_iterations: int = 2
    enable_judge: bool = True
    enable_escalation: bool = True

    # Throughput
    max_concurrent_pages: int = 4
    batch_max_chars: int = 6000
    batch_max_blocks: int = 40
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 3

    # Layout
    min_font_scale: float = 0.72  # shrink text at most to 72% of its original size before flagging overflow

    # Input limits
    max_upload_mb: int = 30
    max_pages: int = 60
    ocr_language: str = "eng"

    data_dir: Path = Path("data")
    job_ttl_hours: int = 24
    openai_tracing: bool = True

    @property
    def backend(self) -> Literal["openai", "pseudo"]:
        if self.llm_backend == "auto":
            return "openai" if self.openai_api_key else "pseudo"
        return self.llm_backend


@lru_cache
def get_settings() -> Settings:
    return Settings()
