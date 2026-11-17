"""ImportService behaviour: import, duplicates, conflicts, audit, rollback, persistence."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from ai_ops_hub.db.engine import create_db_engine, init_db
from ai_ops_hub.db.repositories import AuditRepository, MessageRepository
from ai_ops_hub.dedup import content_fingerprint
from ai_ops_hub.parsing import parse_eml, parse_pasted_text
from ai_ops_hub.schemas import (
    Actor,
    AuditEventType as T,
    DedupDecision,
    ImportStatus,
    SourceKind,
)
from ai_ops_hub.services.import_service import ImportService

from conftest import SAMPLES, pasted, read_sample


def counts(engine):
    with Session(engine) as s:
        return MessageRepository(s).count(), AuditRepository(s).count()


def rows(engine):
    with Session(engine) as s:
        return {m.id: m for m in MessageRepository(s).list()}


@pytest.fixture()
def service(engine, clock):
    return ImportService(engine, clock=clock)


# --- first-time import -----------------------------------------------------------


def test_first_import_stores_message_and_audit_event(service, engine, clock, settings):
    msg = parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings)
    r = service.import_message(msg)

    assert r.decision is DedupDecision.NEW
    assert r.existing_message is None
    m = r.message
    assert m.id == 1 and m.import_status is ImportStatus.IMPORTED
    assert m.source_kind is SourceKind.EML
    assert m.message_id == "inv-2026-0042@zielonadolina.example"
    assert m.dedup_key == "mid:inv-2026-0042@zielonadolina.example"
    assert m.imported_at.utcoffset().total_seconds() == 0
    assert counts(engine) == (1, 1)

    stored = rows(engine)[1]
    assert stored.body_text == msg.body_text
    assert stored.content_fingerprint == content_fingerprint(msg)
    assert stored.raw_sha256 == msg.raw_sha256
    assert stored.message_id_raw == "<inv-2026-0042@Zielonadolina.example>"
    assert stored.sent_at is not None
    assert stored.to_addrs == ["ksiegowosc@fabryka-przykladow.example"]


def test_imported_at_uses_injected_utc_clock(service, clock):
    first = clock.now
    r = service.import_message(pasted())
    assert r.message.imported_at == first


# --- duplicates ------------------------------------------------------------------


def test_identical_reimport_returns_existing_record_and_adds_no_row(service, engine):
    first = service.import_message(pasted())
    again = service.import_message(pasted())

    assert again.decision is DedupDecision.DUPLICATE
    assert again.message.id == first.message.id
    assert again.message.imported_at == first.message.imported_at  # not overwritten
    assert counts(engine)[0] == 1


def test_duplicate_is_audited_on_the_existing_message(service):
    first = service.import_message(pasted())
    service.import_message(pasted())
    service.import_message(pasted())
    types = [e.event_type for e in service.audit_history(first.message.id)]
    assert types == [T.MESSAGE_IMPORTED, T.DUPLICATE_DETECTED, T.DUPLICATE_DETECTED]


def test_same_message_via_eml_and_pasted_text_is_duplicate(service, settings):
    eml = parse_eml(read_sample("03_contract_renewal_en.eml"), settings)
    service.import_message(eml)
    pasted_copy = parse_pasted_text(
        "From: daniel.reeve@northbridge-logistics.example\n"
        "Subject: Renewal of Master Services Agreement MSA-2025-07\n"
        "Message-ID: <msa-renewal-0003@northbridge-logistics.example>\n\n" + eml.body_text
    )
    r = service.import_message(pasted_copy)
    assert r.decision is DedupDecision.DUPLICATE and r.message.source_kind is SourceKind.EML


def test_message_id_domain_case_does_not_create_new_record(service):
    service.import_message(pasted(mid="Abc@EXAMPLE.com"))
    assert service.import_message(pasted(mid="Abc@example.COM")).decision is DedupDecision.DUPLICATE


# --- conflicts -------------------------------------------------------------------


def test_same_message_id_different_content_is_conflict(service, engine, settings):
    original = service.import_message(parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings))
    altered = parse_eml(read_sample("02_invoice_dispute_pl_conflict.eml"), settings)
    r = service.import_message(altered)

    assert r.decision is DedupDecision.CONFLICT
    assert r.message.import_status is ImportStatus.CONFLICT
    assert r.message.id != original.message.id
    assert r.message.conflicts_with_id == original.message.id
    assert r.existing_message.id == original.message.id
    assert counts(engine)[0] == 2


def test_conflict_never_overwrites_existing_message(service, engine, settings):
    service.import_message(parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings))
    before = rows(engine)[1].model_dump()
    service.import_message(parse_eml(read_sample("02_invoice_dispute_pl_conflict.eml"), settings))
    after = rows(engine)
    assert after[1].model_dump() == before
    assert "12 500,00 zł" in after[1].body_text
    # the conflicting content is preserved, not discarded
    assert "21 500,00 zł" in after[2].body_text and after[2].import_status == "conflict"


def test_conflict_is_audited_on_both_records(service):
    a = service.import_message(pasted(body="one"))
    b = service.import_message(pasted(body="two"))
    for mid in (a.message.id, b.message.id):
        events = service.audit_history(mid)
        assert events[-1].event_type is T.CONFLICT_DETECTED
        d = events[-1].details
        assert d["canonical_message_id"] == a.message.id
        assert d["conflicting_message_id"] == b.message.id
        assert d["canonical_fingerprint"] != d["conflicting_fingerprint"]


def test_reimporting_the_same_conflicting_content_is_duplicate_of_the_variant(service, engine):
    service.import_message(pasted(body="one"))
    first = service.import_message(pasted(body="two"))
    again = service.import_message(pasted(body="two"))

    assert again.decision is DedupDecision.DUPLICATE
    assert again.message.id == first.message.id
    assert again.message.import_status is ImportStatus.CONFLICT
    assert again.existing_message.id == 1  # canonical
    assert counts(engine)[0] == 2
    last = service.audit_history(first.message.id)[-1]
    assert last.event_type is T.DUPLICATE_DETECTED
    assert last.details["duplicate_of_import_status"] == "conflict"


def test_each_distinct_variant_gets_its_own_conflict_row(service, engine):
    service.import_message(pasted(body="one"))
    service.import_message(pasted(body="two"))
    service.import_message(pasted(body="three"))
    stored = rows(engine)
    assert [m.import_status for m in stored.values()] == ["imported", "conflict", "conflict"]
    assert {m.conflicts_with_id for m in stored.values() if m.import_status == "conflict"} == {1}


def test_reimporting_original_after_a_conflict_is_still_a_duplicate(service):
    first = service.import_message(pasted(body="one"))
    service.import_message(pasted(body="two"))
    again = service.import_message(pasted(body="one"))
    assert again.decision is DedupDecision.DUPLICATE
    assert again.message.id == first.message.id


# --- missing Message-ID fallback -------------------------------------------------


def test_missing_message_id_uses_content_fallback_key(service, engine, settings):
    msg = parse_eml(read_sample("08_no_message_id_no_date.eml"), settings)
    r = service.import_message(msg)
    assert r.decision is DedupDecision.NEW
    assert r.message.message_id is None
    assert r.message.dedup_key.startswith("fp:")
    assert service.import_message(msg).decision is DedupDecision.DUPLICATE
    assert counts(engine)[0] == 1


def test_missing_message_id_different_content_is_new_not_conflict(service, engine):
    service.import_message(pasted(body="one", mid=None))
    r = service.import_message(pasted(body="two", mid=None))
    assert r.decision is DedupDecision.NEW
    assert counts(engine)[0] == 2


def test_missing_message_id_different_date_header_is_a_different_message(service):
    a = service.import_message(pasted(mid=None, date="Mon, 02 Nov 2026 09:00:00 +0100"))
    b = service.import_message(pasted(mid=None, date="Tue, 03 Nov 2026 09:00:00 +0100"))
    assert a.decision is b.decision is DedupDecision.NEW
    assert a.message.id != b.message.id


# --- audit history ---------------------------------------------------------------


def test_audit_history_is_ordered_and_complete(service, clock):
    first = service.import_message(pasted(body="one"))
    service.import_message(pasted(body="one"))
    service.import_message(pasted(body="two"))
    history = service.audit_history(first.message.id)

    assert [e.event_type for e in history] == [
        T.MESSAGE_IMPORTED, T.DUPLICATE_DETECTED, T.CONFLICT_DETECTED,
    ]
    assert [e.id for e in history] == sorted(e.id for e in history)
    assert all(e.actor is Actor.SYSTEM for e in history)
    ts = [e.timestamp for e in history]
    assert ts == sorted(ts) and all(t.utcoffset().total_seconds() == 0 for t in ts)


def test_audit_details_never_contain_message_body(service):
    secret_body = "UNIQUE-BODY-MARKER-12345"
    first = service.import_message(pasted(body=secret_body))
    service.import_message(pasted(body=secret_body))
    service.import_message(pasted(body=secret_body + " changed"))
    for mid in (1, 2):
        for e in service.audit_history(mid):
            assert "MARKER" not in str(e.details)
    assert first.message.id == 1


def test_actor_is_recorded(service):
    r = service.import_message(pasted(), actor=Actor.HUMAN)
    assert service.audit_history(r.message.id)[0].actor is Actor.HUMAN


def test_reads_for_unknown_ids(service):
    assert service.get_message(999) is None
    assert service.audit_history(999) == []
    assert service.list_messages() == []


# --- transaction rollback --------------------------------------------------------


def test_failure_while_writing_audit_rolls_back_new_message(service, engine, monkeypatch):
    def boom(self, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(AuditRepository, "add", boom)
    with pytest.raises(RuntimeError):
        service.import_message(pasted())
    assert counts(engine) == (0, 0)


def test_failure_halfway_through_conflict_leaves_no_partial_records(service, engine, monkeypatch):
    service.import_message(pasted(body="one"))
    assert counts(engine) == (1, 1)

    original = AuditRepository.add
    calls = {"n": 0}

    def fail_on_second(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # the conflict variant AND first audit were already flushed
            raise RuntimeError("second audit write failed")
        return original(self, **kwargs)

    monkeypatch.setattr(AuditRepository, "add", fail_on_second)
    with pytest.raises(RuntimeError):
        service.import_message(pasted(body="two"))

    assert counts(engine) == (1, 1)  # no variant row, no orphan audit event
    assert calls["n"] == 2


def test_database_error_during_audit_rolls_back_message(service, engine, monkeypatch):
    original = AuditRepository.add

    def bad_foreign_key(self, **kwargs):
        kwargs["message_id"] = 987654  # violates the FK
        return original(self, **kwargs)

    monkeypatch.setattr(AuditRepository, "add", bad_foreign_key)
    with pytest.raises(IntegrityError):
        service.import_message(pasted())
    assert counts(engine) == (0, 0)


def test_service_is_usable_after_a_failed_import(service, engine, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(AuditRepository, "add", lambda self, **kw: (_ for _ in ()).throw(RuntimeError()))
        with pytest.raises(RuntimeError):
            service.import_message(pasted())
    assert service.import_message(pasted()).decision is DedupDecision.NEW
    assert counts(engine) == (1, 1)


def test_lost_race_is_caught_by_db_constraint_and_resolved_by_retry(service, engine, monkeypatch):
    """Simulates a competing import committing between our read and our insert:
    our read is stale (finds nothing), the DB unique index rejects the insert,
    and the retry returns the winner's record as a duplicate."""
    service.import_message(pasted())

    original = MessageRepository.get_canonical
    calls = {"n": 0}

    def stale_first_read(self, key):
        calls["n"] += 1
        return None if calls["n"] == 1 else original(self, key)

    monkeypatch.setattr(MessageRepository, "get_canonical", stale_first_read)
    r = service.import_message(pasted())

    assert r.decision is DedupDecision.DUPLICATE and r.message.id == 1
    assert calls["n"] == 2
    assert counts(engine) == (1, 2)  # failed attempt left nothing behind


# --- persistence after reopening -------------------------------------------------


def test_data_survives_closing_and_reopening_the_database(file_db_url, settings, clock):
    eng1 = create_db_engine(file_db_url, settings)
    init_db(eng1)
    svc1 = ImportService(eng1, clock=clock)
    first = svc1.import_message(pasted(body="one"))
    svc1.import_message(pasted(body="two"))  # conflict
    eng1.dispose()

    eng2 = create_db_engine(file_db_url, settings)
    try:
        init_db(eng2)  # must not wipe anything
        svc2 = ImportService(eng2, clock=clock)
        stored = svc2.list_messages()
        assert [m.import_status for m in stored] == [ImportStatus.IMPORTED, ImportStatus.CONFLICT]
        assert stored[0].imported_at == first.message.imported_at
        assert [e.event_type for e in svc2.audit_history(1)] == [
            T.MESSAGE_IMPORTED, T.CONFLICT_DETECTED,
        ]
        # deduplication still works against data written by the previous session
        again = svc2.import_message(pasted(body="one"))
        assert again.decision is DedupDecision.DUPLICATE and again.message.id == 1
        assert svc2.import_message(pasted(body="three")).decision is DedupDecision.CONFLICT
    finally:
        eng2.dispose()


def test_every_bundled_sample_imports_and_reimports_idempotently(service, engine, settings):
    paths = sorted(p for p in SAMPLES.iterdir() if p.suffix in {".eml", ".txt"})
    parsed = [
        parse_eml(p.read_bytes(), settings) if p.suffix == ".eml"
        else parse_pasted_text(p.read_text(encoding="utf-8"), settings)
        for p in paths
    ]
    first = [service.import_message(m).decision for m in parsed]
    assert first.count(DedupDecision.CONFLICT) == 1  # sample 02 vs 01
    assert first.count(DedupDecision.NEW) == 9
    stored = counts(engine)[0]
    again = [service.import_message(m).decision for m in parsed]
    assert set(again) == {DedupDecision.DUPLICATE}
    assert counts(engine)[0] == stored == 10
