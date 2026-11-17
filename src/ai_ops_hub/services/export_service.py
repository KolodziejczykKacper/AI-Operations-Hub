"""Excel export of approved cases.

What is exported
----------------
* Only cases whose CURRENT status is ``approved`` and whose message is not a conflicting import.
  A reopened case is open again, so it is not exported until it is approved again.
* The values come from the approval snapshot (what the human actually approved), never from
  the raw analysis. Human corrections are part of that snapshot. The snapshot is cross-checked
  against the case's effective data; a case whose records disagree is left out and counted,
  never exported on trust.
* Reading happens inside ONE read transaction (SQLite WAL snapshot), so the exported set is
  internally consistent even if cases change while the file is generated.
* The export is strictly read-only: it never changes a status, a version or any history.
  (No audit event is written: audit events belong to one message, and the server can not know
  whether a generated file was ever downloaded, so it does not claim that.)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from ai_ops_hub.analysis.schemas import AnalysisResult, FieldStatus
from ai_ops_hub.cases import rules
from ai_ops_hub.cases.schemas import EffectiveField
from ai_ops_hub.db.models import AnalysisRecord, CaseRecord, CaseReviewRecord, MessageRecord
from ai_ops_hub.db.repositories import ExportRepository, latest_corrections
from ai_ops_hub.errors import (
    ExportDatabaseError,
    ExportError,
    ExportGenerationError,
    ExportTooLargeError,
    InvalidExportFilterError,
    NoApprovedCasesError,
)
from ai_ops_hub.export.models import Collected, ExportFile, ExportFilter, ExportRow, ExportSummary, ExportValue
from ai_ops_hub.export.workbook import CATEGORY_PL, FIELD_PL, STATUS_PL, build_workbook
from ai_ops_hub.services.import_service import utc_now

log = logging.getLogger("ai_ops_hub.export")

MAX_EXPORT_CASES = 500  # a local demonstration, not a data warehouse

_NOTE_LABELS = {"note": "notatka", "reject": "odrzucenie", "reopen": "ponowne otwarcie"}


def _bounds(flt: ExportFilter) -> tuple[datetime | None, datetime | None]:
    """UTC instants [from, to_exclusive) for the inclusive calendar-day filter."""
    if flt.approved_from and flt.approved_to and flt.approved_from > flt.approved_to:
        raise InvalidExportFilterError("approved_from must not be later than approved_to")
    start = datetime.combine(flt.approved_from, time.min, tzinfo=timezone.utc) if flt.approved_from else None
    end = (
        datetime.combine(flt.approved_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
        if flt.approved_to
        else None
    )
    return start, end


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(timezone.utc) if value else None


class ExportService:
    def __init__(
        self,
        engine: Engine,
        clock: Callable[[], datetime] = utc_now,
        max_cases: int = MAX_EXPORT_CASES,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._max = max_cases

    # --- public API ---------------------------------------------------------------------

    def summary(self, flt: ExportFilter | None = None) -> ExportSummary:
        flt = flt or ExportFilter()
        start, end = _bounds(flt)
        try:
            with Session(self._engine, expire_on_commit=False) as session:
                repo = ExportRepository(session)
                total = repo.count_approved(start, end)
                if total > self._max:
                    eligible, skipped = total, 0  # too many to inspect: the export itself would refuse
                else:
                    collected = self._collect(session, start, end)
                    eligible, skipped = len(collected.rows), len(collected.skipped)
        except SQLAlchemyError as exc:
            raise self._db_error(exc) from exc
        return ExportSummary(
            eligible_count=eligible,
            skipped_count=skipped,
            max_export_cases=self._max,
            exceeds_limit=total > self._max,
            approved_from=flt.approved_from,
            approved_to=flt.approved_to,
        )

    def export(self, flt: ExportFilter | None = None) -> ExportFile:
        flt = flt or ExportFilter()
        start, end = _bounds(flt)
        generated_at = self._clock()
        try:
            with Session(self._engine, expire_on_commit=False) as session:
                total = ExportRepository(session).count_approved(start, end)
                if total > self._max:
                    raise ExportTooLargeError(
                        f"{total} approved cases match; one export holds at most {self._max}. "
                        "Narrow the approval-date range.",
                        eligible=total,
                        limit=self._max,
                    )
                collected = self._collect(session, start, end)
        except SQLAlchemyError as exc:
            raise self._db_error(exc) from exc

        if not collected.rows:
            raise NoApprovedCasesError(
                "there are no approved cases to export"
                + (" for this date range" if start or end else "")
                + (f" ({len(collected.skipped)} approved case(s) were skipped as inconsistent)" if collected.skipped else "")
            )
        try:
            content = build_workbook(
                collected.rows,
                generated_at=generated_at,
                skipped_count=len(collected.skipped),
                approved_from=flt.approved_from,
                approved_to=flt.approved_to,
            )
        except ExportError:
            raise
        except Exception as exc:  # any generation bug must not leak internals or a half-written file
            log.exception("workbook generation failed")
            raise ExportGenerationError("the Excel file could not be generated") from exc
        stamp = _utc(generated_at).strftime("%Y%m%d-%H%M%S")
        return ExportFile(
            content=content,
            filename=f"approved-cases_{stamp}.xlsx",
            case_count=len(collected.rows),
            skipped_count=len(collected.skipped),
            generated_at=generated_at,
        )

    # --- selection ---------------------------------------------------------------------

    @staticmethod
    def _db_error(exc: SQLAlchemyError) -> ExportDatabaseError:
        log.error("database error during export: %s", exc)
        return ExportDatabaseError("the database could not be read for the export")

    def _collect(self, session: Session, start: datetime | None, end: datetime | None) -> Collected:
        repo = ExportRepository(session)
        triples = repo.approved_cases(start, end, self._max + 1)
        ids = [case.id for case, _m, _a in triples]
        corrections = repo.corrections_by_case(ids)
        reviews = repo.reviews_by_case(ids)
        out = Collected()
        for case, message, analysis in triples:
            row = self._row(case, message, analysis, corrections[case.id], reviews[case.id])
            if row is None:
                out.skipped.append(case.id)
            else:
                out.rows.append(row)
        return out

    @staticmethod
    def _row(case: CaseRecord, message: MessageRecord, analysis: AnalysisRecord, corr_rows, review_rows) -> ExportRow | None:
        approval = next(
            (r for r in reversed(review_rows) if r.action == "approve" and r.version_after == case.version),
            None,
        )
        if approval is None or case.decided_at is None:
            return None

        latest = latest_corrections(corr_rows)
        result = AnalysisResult.model_validate(analysis.result)
        category, category_corrected = rules.effective_category(result, latest)
        fields = rules.effective_fields(result, latest)
        if rules.snapshot(category, fields) != approval.snapshot:
            return None  # records disagree: never export on trust

        return ExportRow(
            case_id=case.id,
            version=case.version,
            category=category.value,
            sender=message.from_addr,
            subject=message.subject,
            imported_at=_utc(message.imported_at),
            approved_at=_utc(case.decided_at),
            fields={
                f.name.value: tuple(
                    ExportValue(
                        value=v.value,
                        confirmed=v.origin == "human" or v.status is FieldStatus.VALID,
                        status=v.status.value,
                    )
                    for v in f.values
                )
                for f in fields
                if f.values
            },
            approval_note=approval.note,
            other_notes=_other_notes(review_rows, approval),
            override_used=bool(approval.overridden_problems),
            override_reason=approval.override_reason if approval.overridden_problems else None,
            overridden_problems=tuple(
                f"{p.get('code', '?')}: {p.get('message', '')}" for p in approval.overridden_problems
            ),
            unconfirmed=_unconfirmed(fields),
            corrections=_corrections(result, category, category_corrected, fields),
        )


def _other_notes(reviews: list[CaseReviewRecord], approval: CaseReviewRecord) -> tuple[str, ...]:
    notes = []
    for r in reviews:
        if r.id == approval.id or not r.note or r.action not in _NOTE_LABELS:
            continue
        stamp = _utc(r.created_at).strftime("%Y-%m-%d %H:%M")
        notes.append(f"[{_NOTE_LABELS[r.action]} {stamp} UTC] {r.note}")
    return tuple(notes)


def _unconfirmed(fields: tuple[EffectiveField, ...]) -> tuple[str, ...]:
    out = []
    for f in fields:
        for v in f.values:
            if v.origin == "analyzer" and v.status is not FieldStatus.VALID:
                out.append(f"{FIELD_PL[f.name.value]}: {v.value} ({STATUS_PL[v.status.value]}, analiza regułowa)")
    return tuple(out)


def _corrections(result: AnalysisResult, category, category_corrected: bool, fields) -> tuple[str, ...]:
    out = []
    if category_corrected:
        out.append(
            f"Kategoria: analiza „{CATEGORY_PL.get(result.category.value, result.category.value)}” "
            f"→ człowiek „{CATEGORY_PL.get(category.value, category.value)}”"
        )
    for f in fields:
        if f.corrected:
            before = ", ".join(f.analyzer_values) or "(brak)"
            after = ", ".join(v.value for v in f.values) or "(brak)"
            out.append(f"{FIELD_PL[f.name.value]}: analiza „{before}” → człowiek „{after}”")
    return tuple(out)
