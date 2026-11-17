"""AnalysisService: persistence, audit, conflicts, atomicity, idempotency, interface."""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel

from ai_ops_hub.analysis import AnalysisInput, Analyzer, Category, RuleBasedAnalyzer
from ai_ops_hub.analysis.schemas import (
    AnalysisResult,
    AnalyzedField,
    FieldName,
    ReviewCode,
    ReviewReason,
)
from ai_ops_hub.db.engine import create_db_engine, for_writes, init_db
from ai_ops_hub.db.models import AnalysisRecord, AuditEventRecord, MessageRecord
from ai_ops_hub.db.repositories import AnalysisRepository, AuditRepository, MessageRepository
from ai_ops_hub.errors import AnalyzerContractError, ConflictNotAnalyzableError, MessageNotFoundError
from ai_ops_hub.schemas import (
    Actor,
    AnalysisMethod,
    AuditEventType as T,
    DedupDecision,
    ExtractedValue,
    ImportStatus,
    SourceSpan,
    ValueKind,
)
from ai_ops_hub.services.analysis_service import AnalysisService
from ai_ops_hub.services.import_service import ImportService

from conftest import pasted

ORDER = "Zamawiamy 120 szt. kabli zasilających. Dostawa na 20 listopada 2026. Cena 12 500,00 zł."


def counts(engine):
    with Session(engine) as s:
        return MessageRepository(s).count(), AuditRepository(s).count(), AnalysisRepository(s).count()


def row(engine, mid):
    with Session(engine) as s:
        return MessageRepository(s).get(mid).model_dump()


@pytest.fixture()
def imp(engine, clock):
    return ImportService(engine, clock=clock)


@pytest.fixture()
def svc(engine, clock):
    return AnalysisService(engine, RuleBasedAnalyzer(), clock=clock)


def import_one(imp, body=ORDER, mid="m1@x.example", subject="Zamówienie"):
    return imp.import_message(pasted(body=body, mid=mid, subject=subject)).message


# --------------------------------------------------------------------------- storing an analysis


def test_analysis_is_stored_with_provenance_and_versioning(imp, svc, engine, clock):
    m = import_one(imp)
    out = svc.analyze_message(m.id)

    assert out.created
    a = out.analysis
    assert (a.message_id, a.analyzer_name, a.analyzer_version) == (m.id, "rule_based_analyzer", "1.0.0")
    assert a.analysis_method is AnalysisMethod.RULE_BASED and a.category is Category.ORDER
    assert a.created_at.utcoffset().total_seconds() == 0
    assert a.source_fingerprint == m.content_fingerprint
    assert counts(engine) == (1, 2, 1)

    values = {(f.name.value, f.value.normalized) for f in a.result.fields}
    assert {("quantity", "120 pcs"), ("product", "kabli zasilających"),
            ("requested_date", "2026-11-20"), ("amount", "12500.00 PLN")} <= values
    with Session(engine) as s:
        stored = MessageRepository(s).get(m.id)
    assert a.result.verify_provenance(stored.body_text, stored.subject)  # spans still match the stored message


def test_stored_result_round_trips_exactly(imp, svc):
    m = import_one(imp)
    stored = svc.analyze_message(m.id).analysis
    fresh = RuleBasedAnalyzer().analyze(AnalysisInput(subject="Zamówienie", body_text=pasted(body=ORDER).body_text))
    assert stored.result == fresh
    assert svc.get_analysis(m.id).result == stored.result


def test_analysis_and_its_audit_event_are_written_together(imp, svc, engine, clock):
    m = import_one(imp)
    a = svc.analyze_message(m.id).analysis
    history = imp.audit_history(m.id)
    assert [e.event_type for e in history] == [T.MESSAGE_IMPORTED, T.ANALYSIS_COMPLETED]
    ev = history[-1]
    assert ev.actor is Actor.SYSTEM and ev.timestamp == a.created_at
    d = ev.details
    assert d["analysis_id"] == a.id and d["analyzer_version"] == "1.0.0"
    assert d["analysis_method"] == "rule_based" and d["category"] == "order"
    assert d["requires_review"] is False and d["review_reason_codes"] == [] and d["field_count"] == len(a.result.fields)


def test_audit_details_never_contain_message_text(imp, svc):
    marker = "UNIQUE-BODY-MARKER-98765"
    m = import_one(imp, body=f"Zamawiamy 5 szt. {marker}.")
    svc.analyze_message(m.id)
    for e in imp.audit_history(m.id):
        assert marker.lower() not in str(e.details).lower()


def test_actor_is_recorded(imp, svc):
    m = import_one(imp)
    svc.analyze_message(m.id, actor=Actor.HUMAN)
    assert imp.audit_history(m.id)[-1].actor is Actor.HUMAN


# --------------------------------------------------------------------------- idempotency and versions


def test_rerunning_the_same_analyzer_version_changes_nothing(imp, svc, engine):
    m = import_one(imp)
    first = svc.analyze_message(m.id)
    before = counts(engine)
    again = svc.analyze_message(m.id)
    assert first.created and not again.created
    assert again.analysis.id == first.analysis.id and again.analysis.created_at == first.analysis.created_at
    assert counts(engine) == before  # no new row, no new audit event


def test_a_new_analyzer_version_adds_a_row_and_keeps_history(imp, engine, clock):
    class V2(RuleBasedAnalyzer):
        version = "2.0.0"

    m = import_one(imp)
    v1 = AnalysisService(engine, RuleBasedAnalyzer(), clock=clock)
    v2 = AnalysisService(engine, V2(), clock=clock)
    a1, a2 = v1.analyze_message(m.id), v2.analyze_message(m.id)

    assert a1.created and a2.created and a1.analysis.id != a2.analysis.id
    assert [a.analyzer_version for a in v1.list_analyses(m.id)] == ["1.0.0", "2.0.0"]
    assert v1.get_analysis(m.id).analyzer_version == "1.0.0"  # each service sees its own version
    assert v2.get_analysis(m.id).analyzer_version == "2.0.0"
    assert counts(engine)[2] == 2
    types = [e.event_type for e in imp.audit_history(m.id)]
    assert types.count(T.ANALYSIS_COMPLETED) == 2


def test_import_does_not_analyze_anything_by_itself(imp, engine):
    import_one(imp)
    assert counts(engine) == (1, 1, 0)


# --------------------------------------------------------------------------- separation from import_status


def test_analysis_never_touches_the_message_or_its_import_status(imp, svc, engine):
    m = import_one(imp)
    before = row(engine, m.id)
    svc.analyze_message(m.id)
    assert row(engine, m.id) == before
    assert before["import_status"] == ImportStatus.IMPORTED.value


def test_nothing_is_approved_or_sent_by_analysis(imp, svc, engine):
    m = import_one(imp, body="Zamawiamy 120 szt. kabli zasilających. Dostawa na 20 listopada 2026.")
    out = svc.analyze_message(m.id)
    assert not out.analysis.requires_review  # a clean order: still no approval
    types = {e.event_type.value for e in imp.audit_history(m.id)}
    assert types == {"message_imported", "analysis_completed"}
    assert not any("approv" in t or "send" in t for t in types)
    assert "status" not in {c.name for c in AnalysisRecord.__table__.columns if "case" in c.name}
    assert {c.name for c in AnalysisRecord.__table__.columns}.isdisjoint({"approved", "case_status", "status"})


def test_missing_message_is_reported(svc, engine):
    with pytest.raises(MessageNotFoundError):
        svc.analyze_message(999)
    assert counts(engine) == (0, 0, 0)


# --------------------------------------------------------------------------- conflicting imports


def conflict_pair(imp):
    original = import_one(imp, body="Zamawiamy 10 szt. kabli.")
    imported = imp.import_message(pasted(body="Zamawiamy 99 szt. kabli.", mid="m1@x.example", subject="Zamówienie"))
    assert imported.decision is DedupDecision.CONFLICT
    return original, imported.message


def test_conflicting_import_is_refused_and_leaves_no_trace(imp, svc, engine):
    original, variant = conflict_pair(imp)
    before = counts(engine)
    with pytest.raises(ConflictNotAnalyzableError):
        svc.analyze_message(variant.id)
    assert counts(engine) == before
    assert svc.list_analyses(variant.id) == []
    assert original.id != variant.id


def test_the_analyzer_is_never_even_called_for_a_conflicting_import(imp, engine, clock):
    """Refusing only at write time would be too late for an external (LLM) analyzer:
    the message text would already have been sent. The refusal must come first."""
    calls = []

    class Spy(RuleBasedAnalyzer):
        def analyze(self, message):
            calls.append(message.body_text)
            return super().analyze(message)

    _, variant = conflict_pair(imp)
    spy = AnalysisService(engine, Spy(), clock=clock)
    with pytest.raises(ConflictNotAnalyzableError):
        spy.analyze_message(variant.id)
    assert calls == []
    assert spy.analyze_pending() != [] and variant.id not in [c for c in calls]  # canonical message is analyzed
    assert len(calls) == 1 and "10 szt." in calls[0]  # only the canonical text ever reached the analyzer


def test_database_itself_rejects_an_analysis_of_a_conflict(imp, engine):
    _, variant = conflict_pair(imp)
    result = RuleBasedAnalyzer().analyze(AnalysisInput(body_text="Zamawiamy 99 szt. kabli."))
    with pytest.raises(IntegrityError, match="conflicts"):
        with Session(for_writes(engine)) as s:
            AnalysisRepository(s).add(
                message_id=variant.id, result=result, source_fingerprint="f",
                created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
            s.commit()
    assert counts(engine)[2] == 0


def test_analyze_pending_skips_conflicts_and_is_idempotent(imp, svc, engine):
    a = import_one(imp, body="Zamawiamy 10 szt. kabli.", mid="a@x.example")
    b = import_one(imp, body="Prosimy o kopię faktury FV/2026/0101.", mid="b@x.example")
    original, variant = conflict_pair(imp)  # adds a canonical + a conflict for m1@x.example

    outcomes = svc.analyze_pending()
    analyzed = sorted(o.analysis.message_id for o in outcomes)
    assert analyzed == sorted([a.id, b.id, original.id])
    assert variant.id not in analyzed and all(o.created for o in outcomes)
    assert svc.analyze_pending() == []  # nothing left to do
    assert counts(engine)[2] == 3


def test_explicit_analysis_of_the_canonical_message_is_allowed(imp, svc):
    original, _ = conflict_pair(imp)
    assert svc.analyze_message(original.id).created


# --------------------------------------------------------------------------- atomicity


def test_failure_while_writing_the_audit_event_rolls_back_the_analysis(imp, svc, engine, monkeypatch):
    m = import_one(imp)
    before = counts(engine)

    def boom(self, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(AuditRepository, "add", boom)
    with pytest.raises(RuntimeError):
        svc.analyze_message(m.id)
    assert counts(engine) == before
    monkeypatch.undo()
    assert svc.analyze_message(m.id).created  # nothing half-written blocks a retry


def test_database_error_during_audit_rolls_back_the_analysis(imp, svc, engine, monkeypatch):
    m = import_one(imp)
    before = counts(engine)
    original = AuditRepository.add

    def bad_fk(self, **kwargs):
        kwargs["message_id"] = 987654
        return original(self, **kwargs)

    monkeypatch.setattr(AuditRepository, "add", bad_fk)
    with pytest.raises(IntegrityError):
        svc.analyze_message(m.id)
    assert counts(engine) == before


def test_lost_race_is_resolved_by_returning_the_existing_analysis(imp, engine, clock, monkeypatch):
    """A competing run commits between our read and our write: the unique constraint /
    re-check must yield the winner's row, not a duplicate or a crash."""
    m = import_one(imp)
    winner = AnalysisService(engine, RuleBasedAnalyzer(), clock=clock).analyze_message(m.id)

    svc = AnalysisService(engine, RuleBasedAnalyzer(), clock=clock)
    original = AnalysisRepository.get_for
    calls = {"n": 0}

    def stale_first_read(self, *a, **k):
        calls["n"] += 1
        return None if calls["n"] == 1 else original(self, *a, **k)  # phase-1 read is stale

    monkeypatch.setattr(AnalysisRepository, "get_for", stale_first_read)
    out = svc.analyze_message(m.id)
    assert not out.created and out.analysis.id == winner.analysis.id
    assert counts(engine)[2] == 1


def test_concurrent_analyses_of_one_message_store_exactly_one(file_engine, clock):
    imp = ImportService(file_engine, clock=clock)
    m = import_one(imp)
    svc = AnalysisService(file_engine, RuleBasedAnalyzer(), clock=clock)

    barrier = threading.Barrier(6)

    def work(_):
        barrier.wait(timeout=10)
        return svc.analyze_message(m.id)

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(work, range(6)))

    assert Counter(o.created for o in outcomes) == {True: 1, False: 5}
    assert len({o.analysis.id for o in outcomes}) == 1
    with Session(file_engine) as s:
        assert AnalysisRepository(s).count() == 1
    assert [e.event_type for e in imp.audit_history(m.id)].count(T.ANALYSIS_COMPLETED) == 1


# --------------------------------------------------------------------------- database constraints


def new_result(**over):
    base = dict(
        analyzer_name="x", analyzer_version="1", analysis_method=AnalysisMethod.RULE_BASED,
        category=Category.OTHER, review_reasons=(ReviewReason(code=ReviewCode.NO_CATEGORY_MATCH, message="m"),),
        requires_review=True,
    )
    return AnalysisResult(**{**base, **over})


def insert_analysis(engine, message_id, **record_over):
    from datetime import datetime, timezone

    rec = AnalysisRecord(
        message_id=message_id, analyzer_name="x", analyzer_version="1", analysis_method="rule_based",
        category="other", requires_review=True, result={}, source_fingerprint="f",
        created_at=datetime.now(timezone.utc),
    )
    for k, v in record_over.items():
        setattr(rec, k, v)
    with Session(for_writes(engine)) as s:
        s.add(rec)
        s.commit()


def test_one_analysis_per_message_analyzer_and_version(imp, engine):
    m = import_one(imp)
    insert_analysis(engine, m.id)
    with pytest.raises(IntegrityError):
        insert_analysis(engine, m.id)
    insert_analysis(engine, m.id, analyzer_version="2")  # a different version is fine


def test_analysis_requires_an_existing_message(engine):
    with pytest.raises(IntegrityError):
        insert_analysis(engine, 424242)


@pytest.mark.parametrize("over", [{"analysis_method": "human"}, {"analysis_method": "ai"}, {"category": "refund"}])
def test_check_constraints_reject_unknown_method_and_category(imp, engine, over):
    m = import_one(imp)
    with pytest.raises(IntegrityError):
        insert_analysis(engine, m.id, **over)


def test_a_message_with_analyses_cannot_be_deleted(imp, svc, engine):
    m = import_one(imp)
    svc.analyze_message(m.id)
    with pytest.raises(IntegrityError):
        with Session(for_writes(engine)) as s:
            s.delete(s.get(MessageRecord, m.id))
            s.commit()


def test_existing_stage2_database_gains_the_new_table_without_data_loss(engine, clock):
    """Simulates upgrading a database created before analyses existed."""
    legacy = create_db_engine("sqlite://")
    try:
        SQLModel.metadata.create_all(legacy, tables=[MessageRecord.__table__, AuditEventRecord.__table__])
        with legacy.connect() as c:
            tables = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert "analyses" not in tables

        imp = ImportService(legacy, clock=clock)
        m = import_one(imp)
        before = row(legacy, m.id)
        audit_before = imp.audit_history(m.id)

        init_db(legacy)  # the only migration step: create_all adds what is missing
        with legacy.connect() as c:
            names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master"))}
        assert "analyses" in names and "trg_analyses_no_conflict_insert" in names
        assert row(legacy, m.id) == before and imp.audit_history(m.id) == audit_before

        assert AnalysisService(legacy, RuleBasedAnalyzer(), clock=clock).analyze_message(m.id).created
        init_db(legacy)  # idempotent
        assert counts(legacy)[2] == 1
    finally:
        legacy.dispose()


def test_analysis_survives_reopening_the_database(file_db_url, settings, clock):
    eng = create_db_engine(file_db_url, settings)
    init_db(eng)
    imp = ImportService(eng, clock=clock)
    m = import_one(imp)
    first = AnalysisService(eng, RuleBasedAnalyzer(), clock=clock).analyze_message(m.id).analysis
    eng.dispose()

    eng2 = create_db_engine(file_db_url, settings)
    try:
        init_db(eng2)
        again = AnalysisService(eng2, RuleBasedAnalyzer(), clock=clock).analyze_message(m.id)
        assert not again.created and again.analysis == first
    finally:
        eng2.dispose()


# --------------------------------------------------------------------------- analyzer interface (future Claude adapter)


class FakeLlmAnalyzer:
    """Stands in for a future Claude API adapter: same Protocol, different method label."""

    name = "fake_llm_adapter"
    version = "0.0.1"
    method = AnalysisMethod.LLM

    def analyze(self, message):
        return AnalysisResult(
            analyzer_name=self.name, analyzer_version=self.version, analysis_method=self.method,
            category=Category.COMPLAINT,
            fields=(AnalyzedField(
                name=FieldName.REFERENCE,
                value=ExtractedValue(
                    kind=ValueKind.REFERENCE, normalized="REF-1",
                    span=SourceSpan(start=0, end=1, text=message.body_text[0]), method=AnalysisMethod.LLM,
                ),
            ),),
        )


def test_a_different_analyzer_plugs_in_without_changing_services_or_schema(imp, engine, clock):
    fake = FakeLlmAnalyzer()
    assert isinstance(fake, Analyzer)
    m = import_one(imp)
    out = AnalysisService(engine, fake, clock=clock).analyze_message(m.id)
    assert out.analysis.analysis_method is AnalysisMethod.LLM
    assert out.analysis.analyzer_name == "fake_llm_adapter"
    ev = imp.audit_history(m.id)[-1]
    assert ev.details["analysis_method"] == "llm"
    # ... and the rule-based result for the same message coexists, clearly labelled
    rb = AnalysisService(engine, RuleBasedAnalyzer(), clock=clock).analyze_message(m.id)
    methods = {a.analysis_method for a in AnalysisService(engine, fake).list_analyses(m.id)}
    assert methods == {AnalysisMethod.LLM, AnalysisMethod.RULE_BASED} and rb.created


def test_an_analyzer_that_mislabels_its_method_is_rejected(imp, engine, clock):
    class Liar(FakeLlmAnalyzer):
        method = AnalysisMethod.RULE_BASED  # declares rule_based ...

        def analyze(self, message):  # ... but its results say llm
            return super().analyze(message).model_copy(update={"analysis_method": AnalysisMethod.LLM})

    class ReverseLiar(RuleBasedAnalyzer):
        def analyze(self, message):  # an LLM-style result must not be passed off as rule_based either
            return super().analyze(message).model_copy(update={"analysis_method": AnalysisMethod.LLM})

    m = import_one(imp)
    before = counts(engine)
    for liar in (Liar(), ReverseLiar()):
        with pytest.raises(AnalyzerContractError, match="method"):
            AnalysisService(engine, liar, clock=clock).analyze_message(m.id)
    assert counts(engine) == before


def test_an_analyzer_with_bad_source_spans_is_rejected(imp, engine, clock):
    class BadSpans(FakeLlmAnalyzer):
        def analyze(self, message):
            r = super().analyze(message)
            bad = r.fields[0].model_copy(update={"value": r.fields[0].value.model_copy(
                update={"span": SourceSpan(start=0, end=1, text="#")})})
            return r.model_copy(update={"fields": (bad,)})

    m = import_one(imp)
    before = counts(engine)
    with pytest.raises(AnalyzerContractError, match="span"):
        AnalysisService(engine, BadSpans(), clock=clock).analyze_message(m.id)
    assert counts(engine) == before


def test_an_analyzer_naming_a_different_version_is_rejected(imp, engine, clock):
    class WrongVersion(FakeLlmAnalyzer):
        def analyze(self, message):
            return super().analyze(message).model_copy(update={"analyzer_version": "9.9.9"})

    m = import_one(imp)
    with pytest.raises(AnalyzerContractError):
        AnalysisService(engine, WrongVersion(), clock=clock).analyze_message(m.id)
    assert counts(engine)[2] == 0


def test_the_analyzer_runs_outside_any_database_transaction(imp, engine, clock):
    """A future LLM call must not hold the write lock: while the analyzer runs, another
    writer can still commit immediately."""
    m = import_one(imp)
    other = import_one(imp, body="Zamawiam 3 szt. kabli.", mid="other@x.example")
    seen = {}

    class Probe(RuleBasedAnalyzer):
        version = "probe"

        def analyze(self, message):
            # an independent writer commits while we are "inside" the analyzer
            seen["created"] = AnalysisService(engine, RuleBasedAnalyzer(), clock=clock).analyze_message(other.id).created
            return super().analyze(message)

    AnalysisService(engine, Probe(), clock=clock).analyze_message(m.id)
    assert seen["created"] is True
