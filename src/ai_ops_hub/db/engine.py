"""Explicit SQLite engine configuration.

Every connection gets ``PRAGMA foreign_keys=ON`` and a busy timeout.

Transactions are DEFERRED by default, so read-only work never reserves the
write lock (in WAL mode readers do not block the writer and vice versa).
Code that writes uses ``for_writes(engine)``: its transactions start with
``BEGIN IMMEDIATE``, which takes the write lock up front so a read-check-write
sequence (like an import) is serialised between competing writers instead of
failing halfway with a lock error.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from ai_ops_hub.config import Settings, get_settings


BEGIN_MODE_OPTION = "sqlite_begin_mode"
_BEGIN_MODES = ("DEFERRED", "IMMEDIATE")


def _is_memory(url: str) -> bool:
    db = make_url(url).database
    return db in (None, "", ":memory:")


def create_db_engine(url: str | None = None, settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    url = url or settings.database_url
    if not url.startswith("sqlite"):
        raise ValueError("only SQLite database URLs are supported")

    memory = _is_memory(url)
    kwargs: dict = {}
    if memory:
        # One shared connection, otherwise every connection sees its own empty DB.
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        path = Path(make_url(url).database)
        path.parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"timeout": settings.sqlite_busy_timeout_ms / 1000}

    engine = create_engine(url, **kwargs)
    busy_ms = int(settings.sqlite_busy_timeout_ms)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:
        dbapi_conn.isolation_level = None  # we emit BEGIN ourselves (see below)
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute(f"PRAGMA busy_timeout={busy_ms}")
        if not memory:
            cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn) -> None:
        mode = conn.get_execution_options().get(BEGIN_MODE_OPTION, "DEFERRED")
        if mode not in _BEGIN_MODES:
            raise ValueError(f"unsupported SQLite begin mode: {mode!r}")
        conn.exec_driver_sql(f"BEGIN {mode}")

    return engine


def for_writes(engine: Engine) -> Engine:
    """A view of ``engine`` (same pool and pragmas) whose transactions use
    ``BEGIN IMMEDIATE``. Use it for every unit of work that writes."""
    return engine.execution_options(**{BEGIN_MODE_OPTION: "IMMEDIATE"})


def init_db(engine: Engine) -> None:
    """Create tables if missing. (No migrations yet; Stage 2 is the first schema.)"""
    from ai_ops_hub.db import models  # noqa: F401 - registers tables on the metadata

    SQLModel.metadata.create_all(engine)
