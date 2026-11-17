"""Repositories: thin data access. No deduplication decisions, no commits.

Callers own the transaction (``Session`` commit/rollback). Repositories only
``flush`` so that primary keys and constraint violations surface immediately.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlmodel import Session, col, select

from ai_ops_hub.analysis.schemas import AnalysisResult
from ai_ops_hub.db.models import AnalysisRecord, AuditEventRecord, MessageRecord
from ai_ops_hub.schemas import (
    Actor,
    AuditEventType,
    ImportStatus,
    ParsedMessage,
)


def build_message_record(
    msg: ParsedMessage,
    *,
    dedup_key: str,
    fingerprint: str,
    import_status: ImportStatus,
    imported_at: datetime,
    conflicts_with_id: int | None = None,
) -> MessageRecord:
    # sent_at is stored only when the Date header carried a timezone; a naive
    # value would be a guess. date_raw always keeps the original text.
    sent_at = msg.date if msg.date is not None and msg.date.tzinfo is not None else None
    return MessageRecord(
        message_id=msg.message_id,
        message_id_raw=msg.message_id_raw,
        dedup_key=dedup_key,
        content_fingerprint=fingerprint,
        source_kind=msg.source_kind.value,
        subject=msg.subject,
        from_addr=msg.from_addr,
        to_addrs=list(msg.to_addrs),
        date_raw=msg.date_raw,
        sent_at=sent_at,
        body_text=msg.body_text,
        raw_sha256=msg.raw_sha256,
        missing_headers=list(msg.missing_headers),
        warnings=list(msg.warnings),
        import_status=import_status.value,
        conflicts_with_id=conflicts_with_id,
        imported_at=imported_at,
    )


class MessageRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, record: MessageRecord) -> MessageRecord:
        self._s.add(record)
        self._s.flush()
        return record

    def get(self, record_id: int) -> MessageRecord | None:
        return self._s.get(MessageRecord, record_id)

    def get_canonical(self, dedup_key: str) -> MessageRecord | None:
        stmt = select(MessageRecord).where(
            MessageRecord.dedup_key == dedup_key,
            MessageRecord.import_status != ImportStatus.CONFLICT.value,
        )
        return self._s.exec(stmt).first()

    def get_by_key_and_fingerprint(self, dedup_key: str, fingerprint: str) -> MessageRecord | None:
        stmt = select(MessageRecord).where(
            MessageRecord.dedup_key == dedup_key,
            MessageRecord.content_fingerprint == fingerprint,
        )
        return self._s.exec(stmt).first()

    def list(self, limit: int = 100, offset: int = 0) -> Sequence[MessageRecord]:
        stmt = select(MessageRecord).order_by(col(MessageRecord.id)).offset(offset).limit(limit)
        return self._s.exec(stmt).all()

    def count(self) -> int:
        return len(self._s.exec(select(MessageRecord.id)).all())


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(
        self,
        *,
        message_id: int,
        event_type: AuditEventType,
        actor: Actor,
        timestamp: datetime,
        details: dict,
    ) -> AuditEventRecord:
        record = AuditEventRecord(
            message_id=message_id,
            event_type=event_type.value,
            actor=actor.value,
            timestamp=timestamp,
            details=details,
        )
        self._s.add(record)
        self._s.flush()
        return record

    def list_for_message(self, message_id: int) -> Sequence[AuditEventRecord]:
        stmt = (
            select(AuditEventRecord)
            .where(AuditEventRecord.message_id == message_id)
            .order_by(col(AuditEventRecord.id))
        )
        return self._s.exec(stmt).all()

    def count(self) -> int:
        return len(self._s.exec(select(AuditEventRecord.id)).all())


class AnalysisRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(
        self,
        *,
        message_id: int,
        result: AnalysisResult,
        source_fingerprint: str,
        created_at: datetime,
    ) -> AnalysisRecord:
        record = AnalysisRecord(
            message_id=message_id,
            analyzer_name=result.analyzer_name,
            analyzer_version=result.analyzer_version,
            analysis_method=result.analysis_method.value,
            category=result.category.value,
            requires_review=result.requires_review,
            result=result.model_dump(mode="json"),
            source_fingerprint=source_fingerprint,
            created_at=created_at,
        )
        self._s.add(record)
        self._s.flush()
        return record

    def get_for(self, message_id: int, analyzer_name: str, analyzer_version: str) -> AnalysisRecord | None:
        stmt = select(AnalysisRecord).where(
            AnalysisRecord.message_id == message_id,
            AnalysisRecord.analyzer_name == analyzer_name,
            AnalysisRecord.analyzer_version == analyzer_version,
        )
        return self._s.exec(stmt).first()

    def list_for_message(self, message_id: int) -> Sequence[AnalysisRecord]:
        stmt = (
            select(AnalysisRecord)
            .where(AnalysisRecord.message_id == message_id)
            .order_by(col(AnalysisRecord.id))
        )
        return self._s.exec(stmt).all()

    def pending_message_ids(self, analyzer_name: str, analyzer_version: str, limit: int) -> list[int]:
        """Imported (non-conflict) messages this analyzer version has not analyzed yet."""
        done = select(AnalysisRecord.message_id).where(
            AnalysisRecord.analyzer_name == analyzer_name,
            AnalysisRecord.analyzer_version == analyzer_version,
        )
        stmt = (
            select(MessageRecord.id)
            .where(
                MessageRecord.import_status == ImportStatus.IMPORTED.value,
                col(MessageRecord.id).not_in(done),
            )
            .order_by(col(MessageRecord.id))
            .limit(limit)
        )
        return list(self._s.exec(stmt).all())

    def count(self) -> int:
        return len(self._s.exec(select(AnalysisRecord.id)).all())
