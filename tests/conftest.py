from pathlib import Path

import pytest

from ai_ops_hub.config import Settings

SAMPLES = Path(__file__).parent / "data" / "samples"


@pytest.fixture()
def settings() -> Settings:
    """Isolated settings: ignores any local .env and environment overrides."""
    return Settings(_env_file=None)


@pytest.fixture()
def small_settings() -> Settings:
    return Settings(
        _env_file=None, max_eml_bytes=2_000, max_text_chars=500, max_mime_parts=5, max_headers=10
    )


def read_sample(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()
