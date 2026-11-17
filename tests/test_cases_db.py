"""Database-level guarantees of the case tables (they hold even if the service is bypassed)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from conftest import pasted
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel

from ai_ops_hub.analysis import RuleBasedAnalyzer
from ai_ops_hub.db.engine import create_db_engine, init_db
from ai_ops_hub.db.models import CaseRecord
from ai_ops_hub.services.analysis_service import AnalysisService
from ai_ops_hub.services.case_service import CaseService
from ai_ops_hub.services.import_service import ImportService

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-11-16 12:00:00.000000"


@pytest.fixture()
def svc(engine):
    return ImportService(engine), AnalysisService(engine, RuleBasedAnalyzer()), CaseService(engine)


def _case(svc, body="Please send us the invoice FV/2026/0042 for our records.", mid="db-1@x.example"):
    imp, ana, cases = svc
    m = imp.import_message(pasted(body, mid=mid)).message
    ana.analyze_message(m.id)
    summary, _ = cases.ensure_case(m.id)
    return summary


def _sql(engine, statement: str, **params):
    with engine.begin() as conn:
        return conn.execute(text(statement), params)


def test_a_new_case_is_open_and_at_version_one(svc):
    case = _case(svc)
    assert case.version == 1
    assert case.status.value in {"needs_review", "ready_for_review"}
    assert case.decided_at is None


def test_ensure_case_is_idempotent(svc):
    first = _case(svc)
    again, created = svc[2].ensure_case(first.message_id)
    assert not created and again.id == first.id


def test_ensure_case_needs_an_analysis_first(svc):
    from ai_ops_hub.errors import CaseNotAvailableError

    imp, _, cases = svc
    m = imp.import_message(pasted(mid="noanalysis@x.example")).message
    with pytest.raises(CaseNotAvailableError):
        cases.ensure_case(m.id)


def test_a_conflicting_import_never_becomes_a_case_in_the_service(svc):
    from ai_ops_hub.errors import ConflictNotAnalyzableError

    imp, _, cases = svc
    imp.import_message(pasted("first version", mid="same@x.example"))
    variant = imp.import_message(pasted("different content", mid="same@x.example")).message
    assert variant.import_status.value == "conflict"
    with pytest.raises(ConflictNotAnalyzableError):
        cases.ensure_case(variant.id)


def test_a_conflicting_import_can_not_become_a_case_even_by_direct_sql(engine, svc):
    imp, _, _ = svc
    imp.import_message(pasted("first version", mid="same@x.example"))
    variant = imp.import_message(pasted("different content", mid="same@x.example")).message
    ok = _case(svc, mid="other@x.example")
    with pytest.raises(IntegrityError, match="conflicts must not become cases"):
        _sql(
            engine,
            "INSERT INTO cases (message_id, analysis_id, status, version, created_at, updated_at) "
            "VALUES (:m, :a, 'needs_review', 1, :t, :t)",
            m=variant.id, a=ok.analysis_id, t=STAMP,
        )


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_a_case_can_not_be_born_decided(engine, svc, status):
    ok = _case(svc)
    imp, ana, _ = svc
    other = imp.import_message(pasted("another one", mid="born@x.example")).message
    analysis = ana.analyze_message(other.id).analysis
    with pytest.raises(IntegrityError, match="must start open"):
        _sql(
            engine,
            f"INSERT INTO cases (message_id, analysis_id, status, version, created_at, updated_at, decided_at) "
            f"VALUES (:m, :a, '{status}', 1, :t, :t, :t)",
            m=other.id, a=analysis.id, t=STAMP,
        )
    assert ok.status.value != status


def test_status_can_not_become_approved_by_a_bare_update(engine, svc):
    case = _case(svc)
    with pytest.raises(IntegrityError, match="needs a recorded human decision"):
        _sql(engine, "UPDATE cases SET status='approved', decided_at=:t WHERE id=:i", t=STAMP, i=case.id)


def test_status_can_not_become_rejected_by_a_bare_update(engine, svc):
    case = _case(svc)
    with pytest.raises(IntegrityError, match="needs a recorded human decision"):
        _sql(engine, "UPDATE cases SET status='rejected', decided_at=:t WHERE id=:i", t=STAMP, i=case.id)


def test_a_decision_record_for_the_wrong_action_does_not_unlock_approval(engine, svc):
    case = _case(svc)
    _sql(
        engine,
        "INSERT INTO case_reviews (case_id, action, from_status, to_status, snapshot, overridden_problems, "
        "version_after, reviewer, created_at) VALUES (:i, 'reject', 'needs_review', 'rejected', '{}', '[]', 2, 'x', :t)",
        i=case.id, t=STAMP,
    )
    with pytest.raises(IntegrityError, match="needs a recorded human decision"):
        _sql(engine, "UPDATE cases SET status='approved', version=2, decided_at=:t WHERE id=:i", t=STAMP, i=case.id)


def test_decided_at_must_match_the_status(engine, svc):
    case = _case(svc)
    with pytest.raises(IntegrityError):
        _sql(engine, "UPDATE cases SET decided_at=:t WHERE id=:i", t=STAMP, i=case.id)


def test_unknown_status_is_refused(engine, svc):
    case = _case(svc)
    with pytest.raises(IntegrityError):
        _sql(engine, "UPDATE cases SET status='done' WHERE id=:i", i=case.id)


def test_one_case_per_message(engine, svc):
    case = _case(svc)
    with pytest.raises(IntegrityError):
        _sql(
            engine,
            "INSERT INTO cases (message_id, analysis_id, status, version, created_at, updated_at) "
            "VALUES (:m, :a, 'needs_review', 1, :t, :t)",
            m=case.message_id, a=case.analysis_id, t=STAMP,
        )


def test_the_decision_trigger_accepts_the_service_path(svc):
    case = _case(svc, "Please send us the invoice FV/2026/0042.\n\nBest regards,\nAnna Nowak")
    # approve through the service: review row first, then the guarded status update
    cases = svc[2]
    detail = cases.get_case(case.id)
    out = cases.approve(case.id, detail.case.version, override_reason="checked by hand, all fine")
    assert out.case.status.value == "approved"


@pytest.mark.parametrize("table", ["case_corrections", "case_reviews"])
def test_history_tables_are_append_only(engine, svc, table):
    case = _case(svc)
    cases = svc[2]
    cases.edit_case(case.id, 1, {"product": ["Pump"]}, note="adding a product by hand")
    for statement in (f"UPDATE {table} SET reviewer='someone else'", f"DELETE FROM {table}"):
        with pytest.raises(IntegrityError, match="append-only"):
            _sql(engine, statement)
    assert _sql(engine, f"SELECT COUNT(*) FROM {table}").scalar() >= 1


def test_foreign_keys_are_enforced_for_cases(engine):
    with pytest.raises(IntegrityError):
        _sql(
            engine,
            "INSERT INTO cases (message_id, analysis_id, status, version, created_at, updated_at) "
            "VALUES (999, 999, 'needs_review', 1, :t, :t)",
            t=STAMP,
        )


def test_timestamps_round_trip_as_aware_utc(engine, svc):
    case = _case(svc)
    with Session(engine) as s:
        rec = s.get(CaseRecord, case.id)
        assert rec.created_at.tzinfo is not None and rec.created_at.utcoffset().total_seconds() == 0


# --- additive schema changes ------------------------------------------------------------


def test_init_db_adds_the_case_tables_to_a_stage3_database_without_touching_its_data(file_db_url, settings):
    """A database created by Stage 2/3 code gains the new tables; existing rows survive untouched."""
    from ai_ops_hub.db import models

    old_tables = [
        models.MessageRecord.__table__,
        models.AuditEventRecord.__table__,
        models.AnalysisRecord.__table__,
    ]
    eng = create_db_engine(file_db_url, settings)
    try:
        SQLModel.metadata.create_all(eng, tables=old_tables)  # what Stage 3 would have created
        imp = ImportService(eng)
        msg = imp.import_message(pasted("old data", mid="legacy@x.example")).message
        AnalysisService(eng, RuleBasedAnalyzer()).analyze_message(msg.id)
        with eng.connect() as c:
            before = c.execute(text("SELECT COUNT(*) FROM messages")).scalar(), c.execute(
                text("SELECT COUNT(*) FROM audit_events")
            ).scalar(), c.execute(text("SELECT COUNT(*) FROM analyses")).scalar()
            assert "cases" not in {r[0] for r in c.execute(text("SELECT name FROM sqlite_master"))}

        init_db(eng)  # what create_app does at startup

        with eng.connect() as c:
            names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master"))}
            after = c.execute(text("SELECT COUNT(*) FROM messages")).scalar(), c.execute(
                text("SELECT COUNT(*) FROM audit_events")
            ).scalar(), c.execute(text("SELECT COUNT(*) FROM analyses")).scalar()
        assert {"cases", "case_corrections", "case_reviews"} <= names
        assert before == after
        # the old data is usable by the new layer
        summary, created = CaseService(eng).ensure_case(msg.id)
        assert created and summary.message_id == msg.id
    finally:
        eng.dispose()


def test_init_db_twice_is_harmless(engine):
    init_db(engine)
    init_db(engine)
