"""Typed configuration with hard input-size limits.

Values come from environment variables prefixed ``AIOPS_`` or an optional
``.env`` file (ignored by Git). Unknown keys in ``.env`` (e.g. future API keys)
are ignored, never required.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
