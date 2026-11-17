"""Domain schemas for case management (no I/O, no database types).

A *case* is the human-review workflow of one message. Three things stay
separate: the import state of the message (``imported`` / ``conflict``), what
the analyzer produced (an immutable analysis row) and the review status below.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from ai_ops_hub.analysis.schemas import Category, FieldName, FieldStatus, StoredAnalysis
from ai_ops_hub.schemas import MessageDetail, SourceSpan

# There is no authentication in this local demo, so the reviewer identity is a
# fixed label. It is NOT proof of who acted; it is never taken from the client.
REVIEWER_LABEL = "local_demo_user"

CATEGORY_TARGET = "category"


class ReviewStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"  # open; problems remain in the effective data
    READY_FOR_REVIEW = "ready_for_review"  # open; no known problems, waiting for a human decision
    APPROVED = "approved"  # closed; human decision
    REJECTED = "rejected"  # closed; human decision


OPEN_STATUSES = frozenset({ReviewStatus.NEEDS_REVIEW, ReviewStatus.READY_FOR_REVIEW})
CLOSED_STATUSES = frozenset({ReviewStatus.APPROVED, ReviewStatus.REJECTED})


class ReviewAction(StrEnum):
    NOTE = "note"
    APPROVE = "approve"
    REJECT = "reject"
    REOPEN = "reopen"


# Every legal status change, and the ONLY action that may perform it.
#   triage   - automatic open<->open recomputation after an edit (never a decision)
#   approve / reject / reopen - explicit human actions
LEGAL_TRANSITIONS: dict[tuple[ReviewStatus, ReviewStatus], str] = {
    (ReviewStatus.NEEDS_REVIEW, ReviewStatus.READY_FOR_REVIEW): "triage",
    (ReviewStatus.READY_FOR_REVIEW, ReviewStatus.NEEDS_REVIEW): "triage",
    (ReviewStatus.NEEDS_REVIEW, ReviewStatus.APPROVED): "approve",  # only with a documented override
    (ReviewStatus.READY_FOR_REVIEW, ReviewStatus.APPROVED): "approve",
    (ReviewStatus.NEEDS_REVIEW, ReviewStatus.REJECTED): "reject",
    (ReviewStatus.READY_FOR_REVIEW, ReviewStatus.REJECTED): "reject",
    (ReviewStatus.APPROVED, ReviewStatus.NEEDS_REVIEW): "reopen",
    (ReviewStatus.APPROVED, ReviewStatus.READY_FOR_REVIEW): "reopen",
    (ReviewStatus.REJECTED, ReviewStatus.NEEDS_REVIEW): "reopen",
    (ReviewStatus.REJECTED, ReviewStatus.READY_FOR_REVIEW): "reopen",
}


def is_legal(src: ReviewStatus, dst: ReviewStatus, via: str) -> bool:
    return LEGAL_TRANSITIONS.get((src, dst)) == via


class Problem(BaseModel):
    """Something that must be fixed (or explicitly overridden) before approval."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    field: str | None = None


class EffectiveValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str
    origin: str  # "analyzer" | "human"
    status: FieldStatus
    span: SourceSpan | None = None  # analyzer values keep their source span; human values have none
    warnings: tuple[str, ...] = ()


class EffectiveField(BaseModel):
    """What the case currently says about one field: analyzer values, or the
    human's replacement for them. ``analyzer_values`` is always kept for comparison."""

    model_config = ConfigDict(frozen=True)

    name: FieldName
    corrected: bool
    values: tuple[EffectiveValue, ...]
    analyzer_values: tuple[str, ...]


class CaseSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    message_id: int
    analysis_id: int
    status: ReviewStatus
    version: int
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None
    subject: str | None = None
    effective_category: Category | None = None


class CaseDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    case: CaseSummary
    message: MessageDetail
    analysis: StoredAnalysis
    analyzer_category: Category
    effective_category: Category
    category_corrected: bool
    fields: tuple[EffectiveField, ...]
    problems: tuple[Problem, ...]
    allowed_actions: tuple[str, ...]
    reviewer_label: str = REVIEWER_LABEL


class CorrectionEntry(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: int
    case_id: int
    target: str
    action: str
    values: list[str]
    previous_values: list[str]
    version_after: int
    reviewer: str
    created_at: datetime


class ReviewEntry(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: int
    case_id: int
    action: ReviewAction
    from_status: ReviewStatus
    to_status: ReviewStatus
    note: str | None
    override_reason: str | None
    overridden_problems: list[dict[str, Any]]
    snapshot: dict[str, Any]
    version_after: int
    reviewer: str
    created_at: datetime


class CaseHistory(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: int
    corrections: tuple[CorrectionEntry, ...]
    reviews: tuple[ReviewEntry, ...]
