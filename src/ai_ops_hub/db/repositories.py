"""Repositories: thin data access. No deduplication decisions, no commits.

Callers own the transaction (``Session`` commit/rollback). Repositories only
``flush`` so that primary keys and constraint violations surface immediately.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, or_, update
from sqlmodel import Session, col, select

from ai_ops_hub.analysis.schemas import AnalysisResult
from ai_ops_hub.db.models import (
    AnalysisRecord,
    AuditEventRecord,
    CaseCorrectionRecord,
    CaseRecord,
    CaseReviewRecord,
    LlmCallRecord,
    MessageRecord,
)
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
        return int(self._s.exec(select(func.count()).select_from(MessageRecord)).one())


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

    def latest_for_message(self, message_id: int) -> AnalysisRecord | None:
        stmt = (
            select(AnalysisRecord)
            .where(AnalysisRecord.message_id == message_id)
            .order_by(col(AnalysisRecord.id).desc())
        )
        return self._s.exec(stmt).first()

    def latest_for_message_by_method(self, message_id: int, method: str) -> AnalysisRecord | None:
        stmt = (
            select(AnalysisRecord)
            .where(AnalysisRecord.message_id == message_id, AnalysisRecord.analysis_method == method)
            .order_by(col(AnalysisRecord.id).desc())
        )
        return self._s.exec(stmt).first()

    def get(self, analysis_id: int) -> AnalysisRecord | None:
        return self._s.get(AnalysisRecord, analysis_id)

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


class CaseRepository:
    """Cases, corrections and review decisions. Corrections and reviews are append-only."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, record: CaseRecord) -> CaseRecord:
        self._s.add(record)
        self._s.flush()
        return record

    def get(self, case_id: int) -> CaseRecord | None:
        return self._s.get(CaseRecord, case_id)

    def get_by_message(self, message_id: int) -> CaseRecord | None:
        stmt = select(CaseRecord).where(CaseRecord.message_id == message_id)
        return self._s.exec(stmt).first()

    @staticmethod
    def _filtered(stmt, status: str | None, q: str | None):
        if status:
            stmt = stmt.where(CaseRecord.status == status)
        if q:
            # substring match on subject or sender; LIKE wildcards in the query are escaped
            stmt = stmt.join(MessageRecord, MessageRecord.id == CaseRecord.message_id).where(
                or_(
                    col(MessageRecord.subject).icontains(q, autoescape=True),
                    col(MessageRecord.from_addr).icontains(q, autoescape=True),
                )
            )
        return stmt

    def list(
        self, status: str | None, limit: int, offset: int, q: str | None = None
    ) -> Sequence[CaseRecord]:
        stmt = self._filtered(select(CaseRecord), status, q)
        return self._s.exec(stmt.order_by(col(CaseRecord.id)).offset(offset).limit(limit)).all()

    def count(self, status: str | None = None, q: str | None = None) -> int:
        stmt = self._filtered(select(func.count()).select_from(CaseRecord), status, q)
        return int(self._s.exec(stmt).one())

    def counts_by_status(self) -> dict[str, int]:
        rows = self._s.exec(select(CaseRecord.status, func.count()).group_by(CaseRecord.status)).all()
        return {status: int(n) for status, n in rows}

    def case_index(self, message_ids: Sequence[int]) -> dict[int, CaseRecord]:
        if not message_ids:
            return {}
        stmt = select(CaseRecord).where(col(CaseRecord.message_id).in_(list(message_ids)))
        return {c.message_id: c for c in self._s.exec(stmt).all()}

    def update_if_version(self, case_id: int, expected_version: int, **values) -> bool:
        """Compare-and-set: change the case (and bump its version) only if it is still
        at ``expected_version``. Returns False when someone else got there first."""
        result = self._s.exec(
            update(CaseRecord)
            .where(CaseRecord.id == case_id, CaseRecord.version == expected_version)
            .values(version=expected_version + 1, **values)
            .execution_options(synchronize_session=False)
        )
        self._s.flush()
        self._s.expire_all()
        return result.rowcount == 1

    def add_correction(self, record: CaseCorrectionRecord) -> CaseCorrectionRecord:
        self._s.add(record)
        self._s.flush()
        return record

    def add_review(self, record: CaseReviewRecord) -> CaseReviewRecord:
        self._s.add(record)
        self._s.flush()
        return record

    def corrections(self, case_id: int) -> Sequence[CaseCorrectionRecord]:
        stmt = (
            select(CaseCorrectionRecord)
            .where(CaseCorrectionRecord.case_id == case_id)
            .order_by(col(CaseCorrectionRecord.id))
        )
        return self._s.exec(stmt).all()

    def reviews(self, case_id: int) -> Sequence[CaseReviewRecord]:
        stmt = (
            select(CaseReviewRecord)
            .where(CaseReviewRecord.case_id == case_id)
            .order_by(col(CaseReviewRecord.id))
        )
        return self._s.exec(stmt).all()


class LlmCallRepository:
    """Usage records of optional LLM requests (tokens, cost, latency; never content)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, record: LlmCallRecord) -> LlmCallRecord:
        self._s.add(record)
        self._s.flush()
        return record

    def spent_since(self, since: datetime) -> float:
        """Conservative local spend: the actual cost when known, else the worst-case estimate."""
        stmt = select(
            func.coalesce(func.sum(func.coalesce(LlmCallRecord.actual_cost_usd, LlmCallRecord.estimated_max_cost_usd)), 0.0)
        ).where(col(LlmCallRecord.created_at) >= since)
        return float(self._s.exec(stmt).one())

    def get(self, call_id: int) -> LlmCallRecord | None:
        return self._s.get(LlmCallRecord, call_id)

    def in_flight(self, message_id: int, model: str, since: datetime) -> bool:
        stmt = select(LlmCallRecord.id).where(
            LlmCallRecord.message_id == message_id,
            LlmCallRecord.model == model,
            LlmCallRecord.status == "reserved",
            col(LlmCallRecord.created_at) >= since,
        )
        return self._s.exec(stmt).first() is not None

    def for_message(self, message_id: int) -> Sequence[LlmCallRecord]:
        stmt = select(LlmCallRecord).where(LlmCallRecord.message_id == message_id).order_by(col(LlmCallRecord.id))
        return self._s.exec(stmt).all()

    def for_analysis(self, analysis_id: int) -> LlmCallRecord | None:
        stmt = select(LlmCallRecord).where(LlmCallRecord.analysis_id == analysis_id)
        return self._s.exec(stmt).first()


class ExportRepository:
    """Read-only queries for the Excel export (approved cases only)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _approved(stmt, approved_from: datetime | None, approved_to_exclusive: datetime | None):
        stmt = stmt.join(MessageRecord, MessageRecord.id == CaseRecord.message_id).where(
            CaseRecord.status == "approved",
            MessageRecord.import_status != ImportStatus.CONFLICT.value,
        )
        if approved_from is not None:
            stmt = stmt.where(col(CaseRecord.decided_at) >= approved_from)
        if approved_to_exclusive is not None:
            stmt = stmt.where(col(CaseRecord.decided_at) < approved_to_exclusive)
        return stmt

    def count_approved(self, approved_from: datetime | None, approved_to_exclusive: datetime | None) -> int:
        stmt = self._approved(select(func.count()).select_from(CaseRecord), approved_from, approved_to_exclusive)
        return int(self._s.exec(stmt).one())

    def approved_cases(
        self, approved_from: datetime | None, approved_to_exclusive: datetime | None, limit: int
    ) -> list[tuple[CaseRecord, MessageRecord, AnalysisRecord]]:
        stmt = self._approved(
            select(CaseRecord, MessageRecord, AnalysisRecord).join(
                AnalysisRecord, AnalysisRecord.id == CaseRecord.analysis_id
            ),
            approved_from,
            approved_to_exclusive,
        )
        return [tuple(r) for r in self._s.exec(stmt.order_by(col(CaseRecord.id)).limit(limit)).all()]

    def corrections_by_case(self, case_ids: Sequence[int]) -> dict[int, list[CaseCorrectionRecord]]:
        out: dict[int, list[CaseCorrectionRecord]] = {i: [] for i in case_ids}
        if case_ids:
            stmt = (
                select(CaseCorrectionRecord)
                .where(col(CaseCorrectionRecord.case_id).in_(list(case_ids)))
                .order_by(col(CaseCorrectionRecord.id))
            )
            for r in self._s.exec(stmt).all():
                out[r.case_id].append(r)
        return out

    def reviews_by_case(self, case_ids: Sequence[int]) -> dict[int, list[CaseReviewRecord]]:
        out: dict[int, list[CaseReviewRecord]] = {i: [] for i in case_ids}
        if case_ids:
            stmt = (
                select(CaseReviewRecord)
                .where(col(CaseReviewRecord.case_id).in_(list(case_ids)))
                .order_by(col(CaseReviewRecord.id))
            )
            for r in self._s.exec(stmt).all():
                out[r.case_id].append(r)
        return out


def latest_corrections(rows: Sequence[CaseCorrectionRecord]) -> dict[str, list[str]]:
    """Replay the append-only history: the last 'set' per target wins, a 'revert' clears it."""
    latest: dict[str, list[str]] = {}
    for r in rows:
        if r.action == "set":
            latest[r.target] = list(r.values)
        else:
            latest.pop(r.target, None)
    return latest
