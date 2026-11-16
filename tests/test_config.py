import pytest
from pydantic import ValidationError

from ai_ops_hub.config import Settings


def test_defaults_are_sane():
    s = Settings(_env_file=None)
    assert s.max_eml_bytes == 5 * 1024 * 1024
    assert s.max_text_chars == 200_000


def test_env_override(monkeypatch):
    monkeypatch.setenv("AIOPS_MAX_TEXT_CHARS", "123")
    assert Settings(_env_file=None).max_text_chars == 123


@pytest.mark.parametrize("field", ["max_eml_bytes", "max_text_chars", "max_mime_parts", "max_headers"])
def test_non_positive_limits_rejected(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_absurdly_large_limit_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_eml_bytes=10**12)


def test_settings_are_frozen():
    s = Settings(_env_file=None)
    with pytest.raises(ValidationError):
        s.max_eml_bytes = 1


def test_unknown_env_file_keys_are_ignored():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / ".env"
        env.write_text("ANTHROPIC_API_KEY=not-a-real-key\nAIOPS_MAX_HEADERS=77\n", encoding="utf-8")
        s = Settings(_env_file=env)
    assert s.max_headers == 77
