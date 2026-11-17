"""Typed configuration with hard input-size limits.

Values come from environment variables prefixed ``AIOPS_`` or an optional
``.env`` file (ignored by Git). Unknown keys in ``.env`` (e.g. future API keys)
are ignored, never required.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIOPS_",
        env_file=".env",
        extra="ignore",
        frozen=True,
    )

    max_eml_bytes: int = Field(5 * 1024 * 1024, gt=0, le=50 * 1024 * 1024)
    max_text_chars: int = Field(200_000, gt=0, le=2_000_000)
    max_mime_parts: int = Field(50, gt=0, le=500)
    max_headers: int = Field(300, gt=0, le=2_000)

    # Persistence. Default lives under data/local/, which Git ignores.
    database_url: str = "sqlite:///data/local/ai_ops_hub.db"
    sqlite_busy_timeout_ms: int = Field(5_000, ge=0, le=60_000)

    # Local REST API. Bound to loopback by default; there is no authentication (see README).
    api_host: str = "127.0.0.1"
    api_port: int = Field(8000, ge=1, le=65535)

    # Optional Claude API analysis (Stage 7). DISABLED by default; never used automatically.
    claude_enabled: bool = False
    claude_model: str = "claude-haiku-5-5"  # cheapest supported model; must have a known price
    claude_max_output_tokens: int = Field(4_000, ge=256, le=16_000)
    claude_max_input_chars: int = Field(20_000, ge=100, le=200_000)
    claude_max_cost_usd_per_request: float = Field(0.02, gt=0, le=5)  # worst-case estimate guard
    claude_daily_budget_usd: float = Field(0.25, gt=0, le=100)  # local sum of recorded estimates
    claude_timeout_seconds: float = Field(30.0, gt=0, le=300)
    # The key is read from ANTHROPIC_API_KEY (environment or the ignored .env), not AIOPS_*.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY", repr=False)

    @field_validator("database_url")
    @classmethod
    def _sqlite_only(cls, v: str) -> str:
        if not v.startswith("sqlite"):
            raise ValueError("only SQLite database URLs are supported")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
