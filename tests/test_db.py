"""Database configuration and database-level integrity rules."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlmodel import Session

from ai_ops_hub.config import Settings
from ai_ops_hub.db.engine import create_db_engine, init_db
from ai_ops_hub.db.models import AuditEventRecord, MessageRecord
from ai_ops_hub.db.repositories import build_message_record
from ai_ops_hub.dedup import content_fingerprint, dedup_key
from ai_ops_hub.schemas import ImportStatus

from conftest import pasted

NOW = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)


def record(body="body", mid="m1@x.example", import_status=ImportStatus.IMPORTED, link=None, fp=None):
    msg = pasted(body=body, mid=mid)
    rec = build_message_record(
        msg,
        dedup_key=dedup_key(msg),
        fingerprint=fp or content_fingerprint(msg),
        import_status=import_status,
        imported_at=NOW,
        conflicts_with_id=link,
    )
    return rec


def commit(engine, *records):
    with Session(engine) as s:
        for r in records:
            s.add(r)
        s.commit()


# --- configuration -----------------------------------------------------------


def test_default_database_url_is_under_ignored_data_local():
    assert Settings(_env_file=None).database_url == "sqlite:///data/local/ai_ops_hub.db"


def test_non_sqlite_url_rejected_by_settings_and_engine(settings):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://u:p@h/db")
    with pytest.raises(ValueError):
        create_db_engine("postgresql://u:p@h/db", settings)


def test_foreign_keys_pragma_is_on_for_every_connection(file_engine):
    for _ in range(3):
        with file_engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_foreign_keys_pragma_is_on_in_memory(engine):
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_file_db_uses_wal_and_busy_timeout(file_engine, settings):
    with file_engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == settings.sqlite_busy_timeout_ms


def test_file_db_parent_directories_are_created(file_db_url, db_dir, settings):
    eng = create_db_engine(file_db_url, settings)
    try:
        init_db(eng)
        assert (db_dir / "nested" / "hub.db").exists()
    finally:
        eng.dispose()


def test_init_db_is_idempotent(engine):
    init_db(engine)
    init_db(engine)


# --- UTC handling --------------------------------------------------------------


def test_aware_datetimes_are_stored_as_utc_and_read_back_aware(engine):
    plus2 = timezone(timedelta(hours=2))
    rec = record()
    rec.imported_at = datetime(2026, 11, 16, 12, 0, tzinfo=plus2)
    commit(engine, rec)
    with Session(engine) as s:
        got = s.get(MessageRecord, 1)
        assert got.imported_at == datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        assert got.imported_at.utcoffset() == timedelta(0)
        raw = s.connection().execute(text("SELECT imported_at FROM messages")).scalar()
        assert "10:00:00" in str(raw)  # stored as UTC wall time


def test_naive_datetime_is_rejected(engine):
    rec = record()
    rec.imported_at = datetime(2026, 11, 16, 10, 0)  # naive
    with pytest.raises((ValueError, StatementError)):
        commit(engine, rec)


def test_sent_at_stored_only_when_date_header_has_timezone():
    aware = pasted(date="Mon, 02 Nov 2026 09:15:00 +0100")
    naive = pasted(date="2026-11-02T09:15:00")
    kwargs = dict(fingerprint="f", import_status=ImportStatus.IMPORTED, imported_at=NOW)
    a = build_message_record(aware, dedup_key="k1", **kwargs)
    n = build_message_record(naive, dedup_key="k2", **kwargs)
    assert a.sent_at is not None and a.sent_at.utcoffset() is not None
    assert n.sent_at is None and n.date_raw == "2026-11-02T09:15:00"


# --- uniqueness and integrity enforced by the database ------------------------


def test_same_key_and_fingerprint_cannot_be_stored_twice(engine):
    commit(engine, record())
    with pytest.raises(IntegrityError):
        commit(engine, record())


def test_second_canonical_record_for_same_key_is_rejected(engine):
    commit(engine, record(body="one"))
    with pytest.raises(IntegrityError):
        commit(engine, record(body="two"))  # same Message-ID, different content, not marked conflict


def test_conflict_variants_may_coexist_with_canonical(engine):
    commit(engine, record(body="original"))
    commit(engine, record(body="variant A", import_status=ImportStatus.CONFLICT, link=1))
    commit(engine, record(body="variant B", import_status=ImportStatus.CONFLICT, link=1))
    with Session(engine) as s:
        assert len(s.connection().execute(text("SELECT id FROM messages")).all()) == 3


def test_same_conflicting_content_cannot_be_stored_twice(engine):
    commit(engine, record(body="original"))
    commit(engine, record(body="variant", import_status=ImportStatus.CONFLICT, link=1))
    with pytest.raises(IntegrityError):
        commit(engine, record(body="variant", import_status=ImportStatus.CONFLICT, link=1))


def test_conflict_row_requires_link(engine):
    with pytest.raises(IntegrityError):
        commit(engine, record(import_status=ImportStatus.CONFLICT, link=None))


def test_imported_row_must_not_have_link(engine):
    commit(engine, record(body="original", mid="a@x.example"))
    with pytest.raises(IntegrityError):
        commit(engine, record(body="b", mid="b@x.example", import_status=ImportStatus.IMPORTED, link=1))


def test_conflict_link_must_reference_existing_row(engine):
    with pytest.raises(IntegrityError):  # foreign key enforcement
        commit(engine, record(import_status=ImportStatus.CONFLICT, link=9999))


def test_invalid_import_status_rejected(engine):
    rec = record()
    rec.import_status = "approved"
    with pytest.raises(IntegrityError):
        commit(engine, rec)


def test_audit_event_requires_existing_message(engine):
    ev = AuditEventRecord(
        message_id=424242, event_type="message_imported", actor="system", timestamp=NOW, details={}
    )
    with pytest.raises(IntegrityError):
        commit(engine, ev)


def test_audit_event_with_existing_message_is_accepted(engine):
    commit(engine, record())
    commit(
        engine,
        AuditEventRecord(
            message_id=1, event_type="message_imported", actor="system", timestamp=NOW,
            details={"a": 1},
        ),
    )
    with Session(engine) as s:
        assert s.get(AuditEventRecord, 1).details == {"a": 1}


def test_message_with_audit_history_cannot_be_deleted(engine):
    commit(engine, record())
    commit(
        engine,
        AuditEventRecord(
            message_id=1, event_type="message_imported", actor="system", timestamp=NOW, details={}
        ),
    )
    with pytest.raises(IntegrityError):
        with Session(engine) as s:
            s.delete(s.get(MessageRecord, 1))
            s.commit()


def test_json_columns_roundtrip_unicode(engine):
    rec = record()
    rec.to_addrs = ["zażółć@x.example"]
    rec.warnings = ["ostrzeżenie"]
    commit(engine, rec)
    with Session(engine) as s:
        got = s.get(MessageRecord, 1)
        assert got.to_addrs == ["zażółć@x.example"] and got.warnings == ["ostrzeżenie"]
