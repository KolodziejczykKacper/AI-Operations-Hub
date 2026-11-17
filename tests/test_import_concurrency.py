"""Competing imports against a real file database, each thread with its own session."""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session

from ai_ops_hub.db.repositories import AuditRepository, MessageRepository
from ai_ops_hub.schemas import AuditEventType as T
from ai_ops_hub.schemas import DedupDecision as D
from ai_ops_hub.services.import_service import ImportService

from conftest import pasted


def run_parallel(service, messages):
    barrier = threading.Barrier(len(messages))

    def work(msg):
        barrier.wait(timeout=10)  # release all threads at once
        return service.import_message(msg)

    with ThreadPoolExecutor(max_workers=len(messages)) as pool:
        return list(pool.map(work, messages))


def stored_state(engine):
    with Session(engine) as s:
        msgs = MessageRepository(s).list()
        events = Counter()
        for m in msgs:
            events.update(e.event_type for e in AuditRepository(s).list_for_message(m.id))
        return msgs, events


def test_eight_simultaneous_identical_imports_store_exactly_one_message(file_engine):
    service = ImportService(file_engine)
    results = run_parallel(service, [pasted() for _ in range(8)])

    decisions = Counter(r.decision for r in results)
    assert decisions == {D.NEW: 1, D.DUPLICATE: 7}
    assert {r.message.id for r in results} == {1}

    msgs, events = stored_state(file_engine)
    assert len(msgs) == 1
    assert events[T.MESSAGE_IMPORTED.value] == 1
    assert events[T.DUPLICATE_DETECTED.value] == 7


def test_simultaneous_conflicting_variants_yield_one_canonical_and_one_conflict(file_engine):
    service = ImportService(file_engine)
    batch = [pasted(body="variant A") for _ in range(4)] + [pasted(body="variant B") for _ in range(4)]
    results = run_parallel(service, batch)

    decisions = Counter(r.decision for r in results)
    assert decisions == {D.NEW: 1, D.CONFLICT: 1, D.DUPLICATE: 6}

    msgs, events = stored_state(file_engine)
    assert sorted(m.import_status for m in msgs) == ["conflict", "imported"]
    canonical = next(m for m in msgs if m.import_status == "imported")
    conflict = next(m for m in msgs if m.import_status == "conflict")
    assert conflict.conflicts_with_id == canonical.id
    assert events[T.MESSAGE_IMPORTED.value] == 1
    assert events[T.CONFLICT_DETECTED.value] == 2  # one on each record
    assert events[T.DUPLICATE_DETECTED.value] == 6


def test_simultaneous_imports_of_different_messages_all_succeed(file_engine):
    service = ImportService(file_engine)
    batch = [pasted(body=f"body {i}", mid=f"id{i}@x.example") for i in range(10)]
    results = run_parallel(service, batch)

    assert Counter(r.decision for r in results) == {D.NEW: 10}
    assert len({r.message.id for r in results}) == 10
    msgs, events = stored_state(file_engine)
    assert len(msgs) == 10 and events[T.MESSAGE_IMPORTED.value] == 10


def test_simultaneous_imports_without_message_id_deduplicate_by_content(file_engine):
    service = ImportService(file_engine)
    batch = [pasted(mid=None, body="same text") for _ in range(6)]
    results = run_parallel(service, batch)
    assert Counter(r.decision for r in results) == {D.NEW: 1, D.DUPLICATE: 5}
    assert len(stored_state(file_engine)[0]) == 1
