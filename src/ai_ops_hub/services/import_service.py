"""Idempotent message import with audit events.

One call to ``import_message`` is ONE database transaction: the message row (if
any) and every audit event it produces are committed together or not at all.

Rules (all decisions come from Stage 1 ``dedup.classify``):
* NEW       - store the message, audit ``message_imported``.
* DUPLICATE - same key and content: store nothing, return the existing record,
              audit ``duplicate_detected`` on it.
* CONFLICT  - same key, different content: the existing record is NEVER touched.
              The new content is kept as a separate record with import_status
              ``conflict`` pointing at the canonical one, and
              ``conflict_detected`` is audited on both records.
              Re-importing that same conflicting content later is a DUPLICATE of
              the stored conflict record.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from ai_ops_hub.db.engine import for_writes
from ai_ops_hub.db.models import MessageRecord
from ai_ops_hub.db.repositories import AuditRepository, MessageRepository, build_message_record
from ai_ops_hub.dedup import classify, dedup_key
from ai_ops_hub.schemas import (
    Actor,
    AuditEntry,
    AuditEventType,
    DedupDecision,
    ImportResult,
    ImportStatus,
    ParsedMessage,
    MessageDetail,
    StoredMessage,
)

# A DB-level unique violation means a competing import won the race between our
# read and our insert. One retry re-reads and returns the winner's record.
_MAX_ATTEMPTS = 2


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stored(record: MessageRecord) -> StoredMessage:
    return StoredMessage.model_validate(record)


class ImportService:
    def __init__(self, engine: Engine, clock: Callable[[], datetime] = utc_now) -> None:
        self._engine = engine  # reads: deferred transactions, no write lock
        self._write_engine = for_writes(engine)  # imports: BEGIN IMMEDIATE
        self._clock = clock

    # --- writes -------------------------------------------------------------

    def import_message(self, msg: ParsedMessage, actor: Actor = Actor.SYSTEM) -> ImportResult:
        last_error: IntegrityError | None = None
        for _ in range(_MAX_ATTEMPTS):
            try:
                with Session(self._write_engine, expire_on_commit=False) as session:
                    result = self._import(session, msg, actor)
                    session.commit()
                return result
            except IntegrityError as exc:  # session already rolled back by the context exit
                last_error = exc
        assert last_error is not None
        raise last_error

    def _import(self, session: Session, msg: ParsedMessage, actor: Actor) -> ImportResult:
        messages = MessageRepository(session)
        audits = AuditRepository(session)
        now = self._clock()

        canonical = messages.get_canonical(dedup_key(msg))
        known = {canonical.dedup_key: canonical.content_fingerprint} if canonical else {}
        verdict = classify(msg, known)

        if verdict.decision is DedupDecision.NEW:
            record = messages.add(
                build_message_record(
                    msg,
                    dedup_key=verdict.key,
                    fingerprint=verdict.fingerprint,
                    import_status=ImportStatus.IMPORTED,
                    imported_at=now,
                )
            )
            audits.add(
                message_id=record.id,
                event_type=AuditEventType.MESSAGE_IMPORTED,
                actor=actor,
                timestamp=now,
                details={
                    "dedup_key": verdict.key,
                    "content_fingerprint": verdict.fingerprint,
                    "source_kind": msg.source_kind.value,
                    "raw_sha256": msg.raw_sha256,
                },
            )
            return ImportResult(
                decision=DedupDecision.NEW, message=_stored(record), reason=verdict.reason
            )

        assert canonical is not None  # DUPLICATE / CONFLICT imply a stored canonical record

        if verdict.decision is DedupDecision.DUPLICATE:
            return self._duplicate(
                audits, canonical, canonical, msg, verdict.key, verdict.fingerprint, actor, now,
                reason=verdict.reason,
            )

        # CONFLICT. Was this exact conflicting content stored before?
        variant = messages.get_by_key_and_fingerprint(verdict.key, verdict.fingerprint)
        if variant is not None:
            return self._duplicate(
                audits, variant, canonical, msg, verdict.key, verdict.fingerprint, actor, now,
                reason="identical to a previously stored conflicting variant",
            )

        variant = messages.add(
            build_message_record(
                msg,
                dedup_key=verdict.key,
                fingerprint=verdict.fingerprint,
                import_status=ImportStatus.CONFLICT,
                imported_at=now,
                conflicts_with_id=canonical.id,
            )
        )
        details = {
            "dedup_key": verdict.key,
            "canonical_message_id": canonical.id,
            "conflicting_message_id": variant.id,
            "canonical_fingerprint": canonical.content_fingerprint,
            "conflicting_fingerprint": verdict.fingerprint,
            "reason": verdict.reason,
        }
        for target in (canonical, variant):  # both histories show the conflict
            audits.add(
                message_id=target.id,
                event_type=AuditEventType.CONFLICT_DETECTED,
                actor=actor,
                timestamp=now,
                details=details,
            )
        return ImportResult(
            decision=DedupDecision.CONFLICT,
            message=_stored(variant),
            existing_message=_stored(canonical),
            reason=verdict.reason,
        )

    @staticmethod
    def _duplicate(
        audits: AuditRepository,
        stored: MessageRecord,
        canonical: MessageRecord,
        msg: ParsedMessage,
        key: str,
        fingerprint: str,
        actor: Actor,
        now: datetime,
        *,
        reason: str,
    ) -> ImportResult:
        audits.add(
            message_id=stored.id,
            event_type=AuditEventType.DUPLICATE_DETECTED,
            actor=actor,
            timestamp=now,
            details={
                "dedup_key": key,
                "content_fingerprint": fingerprint,
                "source_kind": msg.source_kind.value,
                "raw_sha256": msg.raw_sha256,
                "duplicate_of_import_status": stored.import_status,
            },
        )
        return ImportResult(
            decision=DedupDecision.DUPLICATE,
            message=_stored(stored),
            existing_message=_stored(canonical) if stored is not canonical else None,
            reason=reason,
        )

    # --- reads --------------------------------------------------------------

    def get_message(self, message_id: int) -> StoredMessage | None:
        with Session(self._engine, expire_on_commit=False) as session:
            record = MessageRepository(session).get(message_id)
            return _stored(record) if record else None

    def get_message_detail(self, message_id: int) -> MessageDetail | None:
        with Session(self._engine, expire_on_commit=False) as session:
            record = MessageRepository(session).get(message_id)
            return MessageDetail.model_validate(record) if record else None

    def count_messages(self) -> int:
        with Session(self._engine, expire_on_commit=False) as session:
            return MessageRepository(session).count()

    def list_messages(self, limit: int = 100, offset: int = 0) -> list[StoredMessage]:
        with Session(self._engine, expire_on_commit=False) as session:
            return [_stored(r) for r in MessageRepository(session).list(limit, offset)]

    def audit_history(self, message_id: int) -> list[AuditEntry]:
        with Session(self._engine, expire_on_commit=False) as session:
            rows = AuditRepository(session).list_for_message(message_id)
            return [AuditEntry.model_validate(r) for r in rows]
