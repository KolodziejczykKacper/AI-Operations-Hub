"""Runs an analyzer over stored messages and persists the result with an audit event.

Design points
-------------
* The analyzer runs OUTSIDE any database transaction (a future LLM adapter must
  not hold the write lock during a network call). Persistence is a second,
  short ``BEGIN IMMEDIATE`` transaction: the analysis row and its audit event
  are committed together or not at all.
* One row per (message, analyzer name, analyzer version). Re-running the same
  version returns the existing row and writes nothing, so it is idempotent; a new
  version adds a row and keeps the history.
* Messages imported as conflicts are never analyzed (service check here, trigger
  in the database).
* Analysis never changes ``import_status`` and has no notion of approval.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from ai_ops_hub.analysis.base import Analyzer
from ai_ops_hub.analysis.schemas import (
    AnalysisInput,
    AnalysisOutcome,
    AnalysisResult,
    StoredAnalysis,
)
from ai_ops_hub.db.engine import for_writes
from ai_ops_hub.db.models import AnalysisRecord
from ai_ops_hub.db.repositories import AnalysisRepository, AuditRepository, MessageRepository
from ai_ops_hub.errors import AnalyzerContractError, ConflictNotAnalyzableError, MessageNotFoundError
from ai_ops_hub.schemas import Actor, AuditEventType, ImportStatus
from ai_ops_hub.services.import_service import utc_now

_MAX_ATTEMPTS = 2


def _stored(record: AnalysisRecord) -> StoredAnalysis:
    return StoredAnalysis(
        id=record.id,
        message_id=record.message_id,
        analyzer_name=record.analyzer_name,
        analyzer_version=record.analyzer_version,
        analysis_method=record.analysis_method,
        category=record.category,
        requires_review=record.requires_review,
        source_fingerprint=record.source_fingerprint,
        created_at=record.created_at,
        result=AnalysisResult.model_validate(record.result),
    )


class AnalysisService:
    def __init__(
        self, engine: Engine, analyzer: Analyzer, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._engine = engine
        self._write_engine = for_writes(engine)
        self._analyzer = analyzer
        self._clock = clock

    # --- writes -------------------------------------------------------------

    def analyze_message(self, message_id: int, actor: Actor = Actor.SYSTEM) -> AnalysisOutcome:
        a = self._analyzer

        # Phase 1 (read-only, deferred): snapshot what the analyzer needs.
        with Session(self._engine, expire_on_commit=False) as session:
            msg = MessageRepository(session).get(message_id)
            if msg is None:
                raise MessageNotFoundError(f"message {message_id} does not exist")
            if msg.import_status == ImportStatus.CONFLICT.value:
                raise ConflictNotAnalyzableError(
                    f"message {message_id} was imported as a conflict and is not analyzed automatically"
                )
            existing = AnalysisRepository(session).get_for(message_id, a.name, a.version)
            if existing is not None:
                return AnalysisOutcome(created=False, analysis=_stored(existing))
            body, subject, sender, fingerprint = msg.body_text, msg.subject, msg.from_addr, msg.content_fingerprint

        # Phase 2: run the analyzer with no transaction open.
        result = a.analyze(AnalysisInput(subject=subject, body_text=body, from_addr=sender))
        self._check_contract(result, body, subject)

        # Phase 3: one short write transaction (analysis row + audit event).
        last_error: IntegrityError | None = None
        for _ in range(_MAX_ATTEMPTS):
            try:
                with Session(self._write_engine, expire_on_commit=False) as session:
                    messages = MessageRepository(session)
                    analyses = AnalysisRepository(session)
                    current = messages.get(message_id)
                    if current is None:
                        raise MessageNotFoundError(f"message {message_id} does not exist")
                    if current.import_status == ImportStatus.CONFLICT.value:
                        raise ConflictNotAnalyzableError(f"message {message_id} is a conflict")
                    raced = analyses.get_for(message_id, a.name, a.version)
                    if raced is not None:  # a competing run finished first
                        return AnalysisOutcome(created=False, analysis=_stored(raced))

                    now = self._clock()
                    record = analyses.add(
                        message_id=message_id,
                        result=result,
                        source_fingerprint=fingerprint,
                        created_at=now,
                    )
                    AuditRepository(session).add(
                        message_id=message_id,
                        event_type=AuditEventType.ANALYSIS_COMPLETED,
                        actor=actor,
                        timestamp=now,
                        details={
                            "analysis_id": record.id,
                            "analyzer_name": a.name,
                            "analyzer_version": a.version,
                            "analysis_method": result.analysis_method.value,
                            "category": result.category.value,
                            "requires_review": result.requires_review,
                            "review_reason_codes": [r.code.value for r in result.review_reasons],
                            "field_count": len(result.fields),
                            "source_fingerprint": fingerprint,
                        },
                    )
                    stored = _stored(record)
                    session.commit()
                return AnalysisOutcome(created=True, analysis=stored)
            except IntegrityError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def analyze_pending(self, limit: int = 100, actor: Actor = Actor.SYSTEM) -> list[AnalysisOutcome]:
        """Analyze imported messages this analyzer version has not seen. Conflicts are skipped."""
        with Session(self._engine, expire_on_commit=False) as session:
            ids = AnalysisRepository(session).pending_message_ids(
                self._analyzer.name, self._analyzer.version, limit
            )
        return [self.analyze_message(i, actor) for i in ids]

    def _check_contract(self, result: AnalysisResult, body: str, subject: str | None) -> None:
        a = self._analyzer
        if result.analysis_method != a.method:
            raise AnalyzerContractError(
                f"analyzer declares method {a.method.value!r} but returned {result.analysis_method.value!r}"
            )
        if (result.analyzer_name, result.analyzer_version) != (a.name, a.version):
            raise AnalyzerContractError("result names a different analyzer name/version than the analyzer")
        if not result.verify_provenance(body, subject):
            raise AnalyzerContractError("a source span does not match the analysed text")

    # --- reads --------------------------------------------------------------

    def list_analyses(self, message_id: int) -> list[StoredAnalysis]:
        with Session(self._engine, expire_on_commit=False) as session:
            return [_stored(r) for r in AnalysisRepository(session).list_for_message(message_id)]

    def get_analysis(self, message_id: int) -> StoredAnalysis | None:
        """The analysis by THIS analyzer's name and version, if any."""
        with Session(self._engine, expire_on_commit=False) as session:
            rec = AnalysisRepository(session).get_for(
                message_id, self._analyzer.name, self._analyzer.version
            )
            return _stored(rec) if rec else None
