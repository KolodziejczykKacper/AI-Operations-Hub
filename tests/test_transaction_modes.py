"""Read-only work must not reserve the write lock; imports must still be atomic
write transactions (BEGIN IMMEDIATE)."""

import threading

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from ai_ops_hub.config import Settings
from ai_ops_hub.db.engine import create_db_engine, for_writes, init_db
from ai_ops_hub.db.repositories import MessageRepository
from ai_ops_hub.schemas import DedupDecision
from ai_ops_hub.services.import_service import ImportService

from conftest import pasted


@pytest.fixture()
def fast_fail_engine(file_db_url):
    """File DB with a tiny busy timeout: any needless lock wait fails the test fast."""
    eng = create_db_engine(file_db_url, Settings(_env_file=None, sqlite_busy_timeout_ms=150))
    init_db(eng)
    yield eng
    eng.dispose()


def capture_begins(engine):
    begins: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        if statement.startswith("BEGIN"):
            begins.append(statement)

    return begins


def test_reads_use_deferred_transactions(engine):
    begins = capture_begins(engine)
    service = ImportService(engine)
    service.list_messages()
    service.get_message(1)
    service.audit_history(1)
    assert begins == ["BEGIN DEFERRED"] * 3


def test_import_uses_begin_immediate(engine):
    begins = capture_begins(engine)
    ImportService(engine).import_message(pasted())
    assert begins == ["BEGIN IMMEDIATE"]  # one transaction for message + audit


def test_for_writes_shares_the_same_database(engine):
    ImportService(engine).import_message(pasted())
    with Session(for_writes(engine)) as s:
        assert MessageRepository(s).count() == 1


def test_open_reader_does_not_block_an_import(fast_fail_engine):
    service = ImportService(fast_fail_engine)
    with Session(fast_fail_engine) as reader:
        reader.connection().execute(text("SELECT count(*) FROM messages")).scalar()  # txn stays open
        # With the old always-IMMEDIATE behaviour this import would wait 150 ms
        # for the reader's write lock and fail with "database is locked".
        result = service.import_message(pasted())
    assert result.decision is DedupDecision.NEW


def test_parallel_readers_do_not_block_each_other(fast_fail_engine):
    ImportService(fast_fail_engine).import_message(pasted())
    with Session(fast_fail_engine) as r1, Session(fast_fail_engine) as r2:
        assert MessageRepository(r1).count() == 1
        assert MessageRepository(r2).count() == 1  # r1 is still open


def test_reader_sees_committed_snapshot_while_writer_is_mid_transaction(fast_fail_engine):
    service = ImportService(fast_fail_engine)
    service.import_message(pasted(body="first", mid="a@x.example"))

    with Session(for_writes(fast_fail_engine)) as writer:
        writer.connection()  # BEGIN IMMEDIATE: write lock held, nothing committed yet
        # a plain reader is not blocked and sees only committed data
        assert len(service.list_messages()) == 1
        writer.rollback()
    assert len(service.list_messages()) == 1


def test_second_writer_waits_for_the_first_instead_of_failing(file_db_url):
    """BEGIN IMMEDIATE serialises writers: the late writer waits (within the busy
    timeout) and then proceeds, rather than failing mid-transaction."""
    eng = create_db_engine(file_db_url, Settings(_env_file=None, sqlite_busy_timeout_ms=5000))
    init_db(eng)
    try:
        service = ImportService(eng)
        first_has_lock = threading.Event()
        release_first = threading.Event()
        outcome: dict = {}

        def holder():
            with Session(for_writes(eng)) as s:
                s.connection()  # takes the write lock
                first_has_lock.set()
                release_first.wait(timeout=10)
                s.rollback()

        def late_writer():
            first_has_lock.wait(timeout=10)
            outcome["result"] = service.import_message(pasted())

        t1, t2 = threading.Thread(target=holder), threading.Thread(target=late_writer)
        t1.start()
        t2.start()
        first_has_lock.wait(timeout=10)
        t2.join(timeout=0.5)
        assert t2.is_alive(), "late writer should be waiting for the write lock"
        release_first.set()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert outcome["result"].decision is DedupDecision.NEW
    finally:
        eng.dispose()


def test_writer_times_out_cleanly_when_lock_is_held_too_long(fast_fail_engine):
    with Session(for_writes(fast_fail_engine)) as holder:
        holder.connection()  # holds the write lock past the 150 ms busy timeout
        with pytest.raises(OperationalError):
            ImportService(fast_fail_engine).import_message(pasted())
        holder.rollback()
    # nothing was written by the failed attempt, and the service still works afterwards
    assert ImportService(fast_fail_engine).list_messages() == []
    assert ImportService(fast_fail_engine).import_message(pasted()).decision is DedupDecision.NEW
