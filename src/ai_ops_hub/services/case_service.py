"""Case management: creation, human corrections, approve / reject / reopen.

Rules this module enforces
--------------------------
* A case exists only for a message that was imported (never a conflict) AND analysed.
* Nothing becomes ``approved`` or ``rejected`` without an explicit call to
  ``approve`` / ``reject``; the system only ever moves a case between the two OPEN
  statuses (triage after an edit), and that is audited as ``system``.
* The original message and the original analysis are never modified. Human edits
  are appended to ``case_corrections``; decisions and notes to ``case_reviews``.
* Every mutation is ONE short ``BEGIN IMMEDIATE`` transaction containing the data
  change, the append-only history rows and the audit event(s): all or nothing.
* Optimistic locking: each mutation names the ``expected_version`` it was based
  on; if the case moved on, nothing is written and ``VersionConflictError`` is raised.
* The reviewer is always ``REVIEWER_LABEL``. It is a label, not an authenticated identity.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Engine
from sqlmodel import Session

from ai_ops_hub.analysis.schemas import AnalysisResult, Category
from ai_ops_hub.cases import rules
from ai_ops_hub.cases.schemas import (
    CATEGORY_TARGET,
    CLOSED_STATUSES,
    OPEN_STATUSES,
    REVIEWER_LABEL,
    CaseDetail,
    CaseHistory,
    CaseStats,
    CaseSummary,
    CorrectionEntry,
    Problem,
    ReviewAction,
    ReviewEntry,
    ReviewStatus,
    is_legal,
)
from ai_ops_hub.db.engine import for_writes
from ai_ops_hub.db.models import (
    AnalysisRecord,
    CaseCorrectionRecord,
    CaseRecord,
    CaseReviewRecord,
)
from ai_ops_hub.db.repositories import (
    AnalysisRepository,
    AuditRepository,
    CaseRepository,
    MessageRepository,
    latest_corrections,
)
from ai_ops_hub.errors import (
    ApprovalBlockedError,
    CaseNotAvailableError,
    CaseNotEditableError,
    CaseNotFoundError,
    ConflictNotAnalyzableError,
    InvalidCorrectionError,
    InvalidTransitionError,
    MessageNotFoundError,
    VersionConflictError,
)
from ai_ops_hub.schemas import Actor, AnalysisMethod, AuditEntry, AuditEventType, ImportStatus, MessageDetail
from ai_ops_hub.services.analysis_service import _stored as _stored_analysis
from ai_ops_hub.services.import_service import utc_now


def _problem_dicts(problems: tuple[Problem, ...]) -> list[dict]:
    return [p.model_dump() for p in problems]


class _Loaded:
    """A case with everything needed to judge it (all read inside one session)."""

    def __init__(
        self, analysis: AnalysisRecord, latest: dict[str, list[str]], case: CaseRecord | None = None
    ) -> None:
        self.case = case
        self.analysis = analysis
        self.result = AnalysisResult.model_validate(analysis.result)
        self.latest = latest
        self.refresh_view()

    @classmethod
    def for_case(cls, case: CaseRecord, analysis: AnalysisRecord, repo: CaseRepository) -> "_Loaded":
        return cls(analysis, latest_corrections(repo.corrections(case.id)), case)

    def refresh_view(self) -> None:
        self.category, self.category_corrected = rules.effective_category(self.result, self.latest)
        self.fields = rules.effective_fields(self.result, self.latest)
        self.problems = rules.find_problems(
            self.result, self.category, self.category_corrected, self.fields, self.latest
        )

    def current_values(self, target: str) -> list[str]:
        if target == CATEGORY_TARGET:
            return [self.category.value]
        for f in self.fields:
            if f.name.value == target:
                return [v.value for v in f.values]
        return []


class CaseService:
    def __init__(self, engine: Engine, clock: Callable[[], datetime] = utc_now) -> None:
        self._engine = engine
        self._write_engine = for_writes(engine)
        self._clock = clock

    # --- creation -----------------------------------------------------------

    def ensure_case(self, message_id: int, analysis_id: int | None = None) -> tuple[CaseSummary, bool]:
        """Return the message's case, creating it (open, never decided) on first use.

        A new case reviews ``analysis_id`` when the caller chooses one explicitly; otherwise it
        reviews the latest RULE-BASED analysis. An optional LLM analysis is never adopted
        implicitly, and an existing case keeps the analysis it was created with.
        """
        with Session(self._write_engine, expire_on_commit=False) as session:
            messages, cases = MessageRepository(session), CaseRepository(session)
            msg = messages.get(message_id)
            if msg is None:
                raise MessageNotFoundError(f"message {message_id} does not exist")
            if msg.import_status == ImportStatus.CONFLICT.value:
                raise ConflictNotAnalyzableError(
                    f"message {message_id} was imported as a conflict and cannot become a case"
                )
            existing = cases.get_by_message(message_id)
            if existing is not None:
                return self._summary(session, existing), False

            analyses = AnalysisRepository(session)
            if analysis_id is not None:
                analysis = analyses.get(analysis_id)
                if analysis is None or analysis.message_id != message_id:
                    raise CaseNotAvailableError(f"analysis {analysis_id} does not belong to message {message_id}")
            else:
                analysis = analyses.latest_for_message_by_method(message_id, AnalysisMethod.RULE_BASED.value)
            if analysis is None:
                raise CaseNotAvailableError(f"message {message_id} has not been analyzed yet")

            now = self._clock()
            probe = _Loaded(analysis, {})  # judge the fresh analysis before the case row exists
            # An analyzer that asked for review always starts as needs_review, whatever else holds.
            status = (
                ReviewStatus.NEEDS_REVIEW
                if probe.result.requires_review
                else rules.triage(probe.problems)
            )
            record = cases.add(
                CaseRecord(
                    message_id=message_id,
                    analysis_id=analysis.id,
                    status=status.value,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            AuditRepository(session).add(
                message_id=message_id,
                event_type=AuditEventType.CASE_CREATED,
                actor=Actor.SYSTEM,
                timestamp=now,
                details={
                    "case_id": record.id,
                    "analysis_id": analysis.id,
                    "analyzer_name": analysis.analyzer_name,
                    "analyzer_version": analysis.analyzer_version,
                    "initial_status": status.value,
                    "analyzer_category": analysis.category,
                    "analysis_method": analysis.analysis_method,
                    "problem_codes": [p.code for p in probe.problems],
                    "note": "a case is only a review workflow; it is never created approved",
                },
            )
            summary = self._summary(session, record)
            session.commit()
        return summary, True

    # --- mutations ----------------------------------------------------------

    def edit_case(
        self,
        case_id: int,
        expected_version: int,
        corrections: dict[str, list[str] | None],
        note: str | None = None,
    ) -> CaseDetail:
        """Apply human corrections ({target: values | None}; None = back to the analyzer's value)
        and/or add a review note. The analysis itself is untouched."""
        if not corrections and not note:
            raise InvalidCorrectionError("nothing to change: give corrections and/or a note")

        with Session(self._write_engine, expire_on_commit=False) as session:
            repo = CaseRepository(session)
            loaded = self._load_for_write(session, repo, case_id, expected_version)
            case = loaded.case
            assert case is not None
            if ReviewStatus(case.status) in CLOSED_STATUSES:
                raise CaseNotEditableError(
                    f"case {case_id} is {case.status}; reopen it before editing"
                )

            now = self._clock()
            old_status = ReviewStatus(case.status)
            new_version = case.version + 1
            changes: list[dict] = []
            validated: dict[str, list[str] | None] = {}
            for target, values in corrections.items():
                validated[target] = (
                    None if values is None else rules.normalize_human_values(target, values)
                )

            for target, values in validated.items():
                before = loaded.current_values(target)
                if values is None:  # revert to the analyzer's value
                    if target not in loaded.latest:
                        continue
                    action, stored = "revert", []
                    loaded.latest.pop(target)
                else:
                    # Re-entering the current value is a no-op, unless it CONFIRMS the analyzer's
                    # value (that is how a human resolves an ambiguous field).
                    if values == before and target in loaded.latest:
                        continue
                    action, stored = "set", values
                    loaded.latest[target] = values
                loaded.refresh_view()
                repo.add_correction(
                    CaseCorrectionRecord(
                        case_id=case_id,
                        target=target,
                        action=action,
                        values=stored,
                        previous_values=before,
                        version_after=new_version,
                        reviewer=REVIEWER_LABEL,
                        created_at=now,
                    )
                )
                changes.append(
                    {"target": target, "action": action, "before": before, "after": loaded.current_values(target)}
                )

            if not changes and not note:
                raise InvalidCorrectionError("no effective change: the values are already what the case says")

            new_status = old_status
            if changes:  # automatic triage between the two OPEN statuses only
                new_status = rules.triage(loaded.problems)
                assert new_status is old_status or is_legal(old_status, new_status, "triage")

            if note:
                repo.add_review(
                    CaseReviewRecord(
                        case_id=case_id,
                        action=ReviewAction.NOTE.value,
                        from_status=old_status.value,
                        to_status=new_status.value,
                        note=note,
                        snapshot=rules.snapshot(loaded.category, loaded.fields),
                        version_after=new_version,
                        reviewer=REVIEWER_LABEL,
                        created_at=now,
                    )
                )
            self._commit_version(
                repo, case, status=new_status.value, updated_at=now
            )

            audits = AuditRepository(session)
            if changes:
                audits.add(
                    message_id=case.message_id,
                    event_type=AuditEventType.CASE_UPDATED,
                    actor=Actor.HUMAN,
                    timestamp=now,
                    details={
                        "case_id": case_id,
                        "reviewer": REVIEWER_LABEL,
                        "version_before": new_version - 1,
                        "version_after": new_version,
                        "changes": changes,
                        "remaining_problem_codes": [p.code for p in loaded.problems],
                    },
                )
            if new_status is not old_status:
                audits.add(
                    message_id=case.message_id,
                    event_type=AuditEventType.STATUS_CHANGED,
                    actor=Actor.SYSTEM,  # automatic triage after an edit, not a decision
                    timestamp=now,
                    details={
                        "case_id": case_id,
                        "from": old_status.value,
                        "to": new_status.value,
                        "reason": "triage after human correction",
                        "version_after": new_version,
                    },
                )
            if note:
                audits.add(
                    message_id=case.message_id,
                    event_type=AuditEventType.CASE_NOTE_ADDED,
                    actor=Actor.HUMAN,
                    timestamp=now,
                    details={
                        "case_id": case_id,
                        "reviewer": REVIEWER_LABEL,
                        "note": note,
                        "version_after": new_version,
                    },
                )
            detail = self._detail(session, case.id)
            session.commit()
        return detail

    def approve(
        self,
        case_id: int,
        expected_version: int,
        note: str | None = None,
        override_reason: str | None = None,
    ) -> CaseDetail:
        """Approve. The EFFECTIVE data (after human corrections) must have no problems,
        unless the human gives an explicit written ``override_reason``."""
        with Session(self._write_engine, expire_on_commit=False) as session:
            repo = CaseRepository(session)
            loaded = self._load_for_write(session, repo, case_id, expected_version)
            case = loaded.case
            old_status = ReviewStatus(case.status)
            self._require_open(case, "approve")

            overridden: list[dict] = []
            if loaded.problems:
                if not override_reason:
                    raise ApprovalBlockedError(
                        "the case data still has problems; correct them or give an explicit override_reason",
                        _problem_dicts(loaded.problems),
                    )
                overridden = _problem_dicts(loaded.problems)

            now = self._clock()
            new_version = case.version + 1
            snap = rules.snapshot(loaded.category, loaded.fields)
            repo.add_review(
                CaseReviewRecord(
                    case_id=case_id,
                    action=ReviewAction.APPROVE.value,
                    from_status=old_status.value,
                    to_status=ReviewStatus.APPROVED.value,
                    note=note,
                    override_reason=override_reason if overridden else None,
                    overridden_problems=overridden,
                    snapshot=snap,
                    version_after=new_version,
                    reviewer=REVIEWER_LABEL,
                    created_at=now,
                )
            )
            self._commit_version(
                repo, case, status=ReviewStatus.APPROVED.value, updated_at=now, decided_at=now
            )
            AuditRepository(session).add(
                message_id=case.message_id,
                event_type=AuditEventType.CASE_APPROVED,
                actor=Actor.HUMAN,
                timestamp=now,
                details={
                    "case_id": case_id,
                    "reviewer": REVIEWER_LABEL,
                    "from": old_status.value,
                    "to": ReviewStatus.APPROVED.value,
                    "version_after": new_version,
                    "override_used": bool(overridden),
                    "override_reason": override_reason if overridden else None,
                    "overridden_problem_codes": [p["code"] for p in overridden],
                    "note": note,
                    "analysis_id": case.analysis_id,
                    "approved_data": snap,
                },
            )
            detail = self._detail(session, case.id)
            session.commit()
        return detail

    def reject(self, case_id: int, expected_version: int, reason: str) -> CaseDetail:
        return self._decide(case_id, expected_version, ReviewAction.REJECT, reason)

    def reopen(self, case_id: int, expected_version: int, reason: str) -> CaseDetail:
        return self._decide(case_id, expected_version, ReviewAction.REOPEN, reason)

    def _decide(self, case_id: int, expected_version: int, action: ReviewAction, reason: str) -> CaseDetail:
        with Session(self._write_engine, expire_on_commit=False) as session:
            repo = CaseRepository(session)
            loaded = self._load_for_write(session, repo, case_id, expected_version)
            case = loaded.case
            old_status = ReviewStatus(case.status)
            now = self._clock()
            new_version = case.version + 1

            if action is ReviewAction.REJECT:
                self._require_open(case, "reject")
                new_status, event, decided_at = ReviewStatus.REJECTED, AuditEventType.CASE_REJECTED, now
            else:
                if old_status not in CLOSED_STATUSES:
                    raise InvalidTransitionError(f"case {case_id} is {old_status.value}; only a decided case can be reopened")
                new_status, event, decided_at = rules.triage(loaded.problems), AuditEventType.CASE_REOPENED, None
            assert is_legal(old_status, new_status, action.value)

            snap = rules.snapshot(loaded.category, loaded.fields)
            repo.add_review(
                CaseReviewRecord(
                    case_id=case_id,
                    action=action.value,
                    from_status=old_status.value,
                    to_status=new_status.value,
                    note=reason,
                    snapshot=snap,
                    version_after=new_version,
                    reviewer=REVIEWER_LABEL,
                    created_at=now,
                )
            )
            self._commit_version(repo, case, status=new_status.value, updated_at=now, decided_at=decided_at)
            AuditRepository(session).add(
                message_id=case.message_id,
                event_type=event,
                actor=Actor.HUMAN,
                timestamp=now,
                details={
                    "case_id": case_id,
                    "reviewer": REVIEWER_LABEL,
                    "from": old_status.value,
                    "to": new_status.value,
                    "reason": reason,
                    "version_after": new_version,
                    "data": snap,
                },
            )
            detail = self._detail(session, case.id)
            session.commit()
        return detail

    # --- helpers for mutations ---------------------------------------------

    @staticmethod
    def _load_for_write(session: Session, repo: CaseRepository, case_id: int, expected_version: int) -> _Loaded:
        case = repo.get(case_id)
        if case is None:
            raise CaseNotFoundError(f"case {case_id} does not exist")
        if case.version != expected_version:
            raise VersionConflictError(
                f"case {case_id} is at version {case.version}, but the request was based on "
                f"version {expected_version}; reload the case and retry",
                current_version=case.version,
            )
        analysis = AnalysisRepository(session).get(case.analysis_id)
        assert analysis is not None  # FK guarantees it
        return _Loaded.for_case(case, analysis, repo)

    @staticmethod
    def _require_open(case: CaseRecord, verb: str) -> None:
        if ReviewStatus(case.status) not in OPEN_STATUSES:
            raise InvalidTransitionError(
                f"case {case.id} is already {case.status}; cannot {verb} it (reopen first)"
            )

    @staticmethod
    def _commit_version(repo: CaseRepository, case: CaseRecord, **values) -> None:
        """Compare-and-set the case row; also bumps ``version`` by one."""
        expected = case.version
        if not repo.update_if_version(case.id, expected, **values):
            current = repo.get(case.id)
            raise VersionConflictError(
                f"case {case.id} was changed by someone else; reload and retry",
                current_version=current.version if current else expected,
            )

    # --- reads --------------------------------------------------------------

    def get_case(self, case_id: int) -> CaseDetail:
        with Session(self._engine, expire_on_commit=False) as session:
            return self._detail(session, case_id)

    def get_case_for_message(self, message_id: int) -> CaseDetail | None:
        with Session(self._engine, expire_on_commit=False) as session:
            case = CaseRepository(session).get_by_message(message_id)
            return self._detail(session, case.id) if case else None

    def list_cases(
        self, status: ReviewStatus | None, limit: int, offset: int, q: str | None = None
    ) -> tuple[list[CaseSummary], int]:
        with Session(self._engine, expire_on_commit=False) as session:
            repo = CaseRepository(session)
            value = status.value if status else None
            rows = repo.list(value, limit, offset, q)
            return [self._summary(session, r) for r in rows], repo.count(value, q)

    def stats(self) -> CaseStats:
        """Exact counts per review status (one grouped query, no pagination involved)."""
        with Session(self._engine, expire_on_commit=False) as session:
            counts = CaseRepository(session).counts_by_status()
        by_status = {s: counts.get(s.value, 0) for s in ReviewStatus}
        return CaseStats(total=sum(by_status.values()), by_status=by_status)

    def case_index(self, message_ids: list[int]) -> dict[int, tuple[int, ReviewStatus]]:
        """message_id -> (case_id, status) for the messages that have a case."""
        with Session(self._engine, expire_on_commit=False) as session:
            idx = CaseRepository(session).case_index(message_ids)
            return {m: (c.id, ReviewStatus(c.status)) for m, c in idx.items()}

    def history(self, case_id: int) -> CaseHistory:
        with Session(self._engine, expire_on_commit=False) as session:
            repo = CaseRepository(session)
            if repo.get(case_id) is None:
                raise CaseNotFoundError(f"case {case_id} does not exist")
            return CaseHistory(
                case_id=case_id,
                corrections=tuple(CorrectionEntry.model_validate(r) for r in repo.corrections(case_id)),
                reviews=tuple(ReviewEntry.model_validate(r) for r in repo.reviews(case_id)),
            )

    def audit(self, case_id: int) -> list[AuditEntry]:
        """Audit events of the case's message (import, analysis and every case event)."""
        with Session(self._engine, expire_on_commit=False) as session:
            case = CaseRepository(session).get(case_id)
            if case is None:
                raise CaseNotFoundError(f"case {case_id} does not exist")
            rows = AuditRepository(session).list_for_message(case.message_id)
            return [AuditEntry.model_validate(r) for r in rows]

    # --- view building ------------------------------------------------------

    def _summary(self, session: Session, case: CaseRecord) -> CaseSummary:
        msg = MessageRepository(session).get(case.message_id)
        analysis = AnalysisRepository(session).get(case.analysis_id)
        corrections = latest_corrections(CaseRepository(session).corrections(case.id))
        category = (
            Category(corrections[CATEGORY_TARGET][0])
            if CATEGORY_TARGET in corrections
            else Category(analysis.category)
        )
        return CaseSummary(
            id=case.id,
            message_id=case.message_id,
            analysis_id=case.analysis_id,
            status=ReviewStatus(case.status),
            version=case.version,
            created_at=case.created_at,
            updated_at=case.updated_at,
            decided_at=case.decided_at,
            subject=msg.subject if msg else None,
            effective_category=category,
            imported_at=msg.imported_at if msg else None,
        )

    def _detail(self, session: Session, case_id: int) -> CaseDetail:
        repo = CaseRepository(session)
        case = repo.get(case_id)
        if case is None:
            raise CaseNotFoundError(f"case {case_id} does not exist")
        analysis = AnalysisRepository(session).get(case.analysis_id)
        loaded = _Loaded.for_case(case, analysis, repo)
        message = MessageRepository(session).get(case.message_id)
        status = ReviewStatus(case.status)
        return CaseDetail(
            case=self._summary(session, case),
            message=MessageDetail.model_validate(message),
            analysis=_stored_analysis(analysis),
            analyzer_category=loaded.result.category,
            effective_category=loaded.category,
            category_corrected=loaded.category_corrected,
            fields=loaded.fields,
            problems=loaded.problems,
            allowed_actions=rules.allowed_actions(status, loaded.problems),
        )
