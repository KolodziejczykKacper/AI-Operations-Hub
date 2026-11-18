"""Typed configuration with hard input-size limits.

Values come from environment variables prefixed ``AIOPS_`` or an optional
``.env`` file (ignored by Git). Unknown keys in ``.env`` (e.g. future API keys)
are ignored, never required.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


OFFICIAL_CLAUDE_BASE_URL = "https://api.anthropic.com"


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
    # The API key is only ever sent to this address. The SDK would otherwise obey the ANTHROPIC_BASE_URL environment
    # variable and send the key wherever it points, so that variable is ignored. A non-official address needs an
    # explicit opt-in and https.
    claude_base_url: str = OFFICIAL_CLAUDE_BASE_URL
    claude_allow_custom_base_url: bool = False
    # The key is read from ANTHROPIC_API_KEY (environment or the ignored .env), not AIOPS_*.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY", repr=False)

    @field_validator("claude_base_url")
    @classmethod
    def _https_base_url(cls, v: str) -> str:
        parts = urlsplit(v.strip())
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("claude_base_url must be a plain https URL (no credentials, query or fragment)")
        return v.strip().rstrip("/")

    @model_validator(mode="after")
    def _custom_endpoint_needs_opt_in(self) -> "Settings":
        host = urlsplit(self.claude_base_url).hostname
        if host != urlsplit(OFFICIAL_CLAUDE_BASE_URL).hostname and not self.claude_allow_custom_base_url:
            raise ValueError(
                "claude_base_url is not the official Anthropic address; set AIOPS_CLAUDE_ALLOW_CUSTOM_BASE_URL=true "
                "if you really want your API key sent there"
            )
        return self

    @field_validator("database_url")
    @classmethod
    def _sqlite_only(cls, v: str) -> str:
        if not v.startswith("sqlite"):
            raise ValueError("only SQLite database URLs are supported")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
