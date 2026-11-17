import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_ops_hub.config import Settings
from ai_ops_hub.db.engine import create_db_engine, init_db
from ai_ops_hub.parsing import parse_pasted_text

SAMPLES = Path(__file__).parent / "data" / "samples"


class StepClock:
    """Deterministic UTC clock: every call advances one minute."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 16, 10, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.now
        self.now += timedelta(minutes=1)
        return current


@pytest.fixture()
def clock() -> StepClock:
    return StepClock()


@pytest.fixture()
def engine(settings):
    """In-memory SQLite engine with the real pragmas and tables."""
    eng = create_db_engine("sqlite://", settings)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db_dir():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        yield Path(d)


@pytest.fixture()
def file_db_url(db_dir) -> str:
    return f"sqlite:///{(db_dir / 'nested' / 'hub.db').as_posix()}"


@pytest.fixture()
def file_engine(file_db_url, settings):
    eng = create_db_engine(file_db_url, settings)
    init_db(eng)
    yield eng
    eng.dispose()


def pasted(body: str = "Please confirm the order.", mid: str | None = "m1@x.example",
           subject: str = "Order", date: str | None = None):
    """Build a ParsedMessage from fictional pasted text."""
    lines = ["From: tester@zielonadolina.example", f"Subject: {subject}"]
    if mid:
        lines.append(f"Message-ID: <{mid}>")
    if date:
        lines.append(f"Date: {date}")
    return parse_pasted_text("\n".join(lines) + "\n\n" + body)


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
