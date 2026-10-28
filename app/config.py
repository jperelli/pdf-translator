from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
}


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

    # USD per 1M tokens (input, output). OpenAI list prices, Oct 2026. Override with MODEL_PRICES='{"m": [in, out]}'.
    model_prices: str = ""

    data_dir: Path = Path("data")
    job_ttl_hours: int = 24
    openai_tracing: bool = True

    def price(self, model: str) -> tuple[float, float]:
        table = {**PRICES, **{k: tuple(v) for k, v in (json.loads(self.model_prices) if self.model_prices else {}).items()}}
        return table.get(model, (0.0, 0.0))

    def for_purpose(self, purpose: str) -> "Settings":
        """'understand' is a cheap draft; 'publish' is the professional pass. Overlaps are blocked in both."""
        if purpose == "understand":
            return self.model_copy(update={"min_font_scale": 0.6, "enable_escalation": False,
                                           "judge_model": self.translator_model, "max_refine_iterations": 1})
        return self.model_copy(update={"min_font_scale": max(self.min_font_scale, 0.8)})

    @property
    def backend(self) -> Literal["openai", "pseudo"]:
        if self.llm_backend == "auto":
            return "openai" if self.openai_api_key else "pseudo"
        return self.llm_backend


@lru_cache
def get_settings() -> Settings:
    return Settings()
