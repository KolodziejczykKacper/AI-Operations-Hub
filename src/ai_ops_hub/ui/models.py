"""Response models of the UI's API client.

Deliberately independent of the backend's domain classes: the UI only knows the JSON
contract of the HTTP API. Unknown fields are ignored, so a backend that gains fields
does not break the UI; a missing or mistyped field is reported as an invalid response.
Statuses and categories stay plain strings for the same reason.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Health(_M):
    status: str
    database: str = "unknown"


class Stats(_M):
    total: int
    by_status: dict[str, int]


class StoredMessage(_M):
    id: int
    import_status: str
    subject: str | None = None
    from_addr: str | None = None
    source_kind: str = "unknown"
    imported_at: datetime
    conflicts_with_id: int | None = None


class MessageListItem(StoredMessage):
    case_id: int | None = None
    case_status: str | None = None


class CaseSummary(_M):
    id: int
    message_id: int
    analysis_id: int
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None = None
    subject: str | None = None
    effective_category: str | None = None
    imported_at: datetime | None = None


class Page(_M, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ImportResult(_M):
    decision: str  # new | duplicate | conflict
    message: StoredMessage
    existing_message: StoredMessage | None = None
    reason: str = ""


class Span(_M):
    start: int
    end: int
    text: str
    source: str = "body"


class WarningItem(_M):
    code: str
    message: str = ""
    span: Span | None = None


class AnalyzedFieldValue(_M):
    normalized: str
    span: Span
    method: str = "rule_based"


class AnalyzedField(_M):
    name: str
    value: AnalyzedFieldValue
    status: str
    warnings: list[WarningItem] = Field(default_factory=list)


class ReviewReason(_M):
    code: str
    message: str = ""


class AnalysisResult(_M):
    category_alternatives: list[str] = Field(default_factory=list)
    fields: list[AnalyzedField] = Field(default_factory=list)
    warnings: list[WarningItem] = Field(default_factory=list)
    review_reasons: list[ReviewReason] = Field(default_factory=list)


class Analysis(_M):
    id: int
    message_id: int
    analyzer_name: str
    analyzer_version: str
    analysis_method: str
    category: str
    requires_review: bool
    created_at: datetime
    result: AnalysisResult


class AnalyzeResponse(_M):
    analysis_created: bool
    case_created: bool
    case: CaseSummary
    analysis: Analysis


class MessageDetail(_M):
    id: int
    subject: str | None = None
    from_addr: str | None = None
    to_addrs: list[str] = Field(default_factory=list)
    date_raw: str | None = None
    body_text: str
    import_status: str
    source_kind: str = "unknown"
    imported_at: datetime
    warnings: list[str] = Field(default_factory=list)


class MessageView(_M):
    message: MessageDetail
    case_id: int | None = None
    case_status: str | None = None
    analysis_count: int = 0


class EffectiveValue(_M):
    value: str
    origin: str  # analyzer | human
    status: str
    span: Span | None = None
    warnings: list[str] = Field(default_factory=list)


class EffectiveField(_M):
    name: str
    corrected: bool
    values: list[EffectiveValue]
    analyzer_values: list[str]


class Problem(_M):
    code: str
    message: str
    field: str | None = None


class CaseDetail(_M):
    case: CaseSummary
    message: MessageDetail
    analysis: Analysis
    analyzer_category: str
    effective_category: str
    category_corrected: bool
    fields: list[EffectiveField]
    problems: list[Problem]
    allowed_actions: list[str]
    reviewer_label: str = "local_demo_user"


class CorrectionEntry(_M):
    id: int
    target: str
    action: str
    values: list[str]
    previous_values: list[str]
    version_after: int
    reviewer: str
    created_at: datetime


class ReviewEntry(_M):
    id: int
    action: str
    from_status: str
    to_status: str
    note: str | None = None
    override_reason: str | None = None
    overridden_problems: list[dict[str, Any]] = Field(default_factory=list)
    version_after: int
    reviewer: str
    created_at: datetime


class History(_M):
    case_id: int
    corrections: list[CorrectionEntry]
    reviews: list[ReviewEntry]


class AuditEntry(_M):
    id: int
    event_type: str
    actor: str
    timestamp: datetime
    details: dict[str, Any] = Field(default_factory=dict)
