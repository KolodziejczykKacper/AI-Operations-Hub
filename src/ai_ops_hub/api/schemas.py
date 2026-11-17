"""Request and response models of the HTTP layer.

Requests forbid unknown fields on purpose: a client can NOT supply an "actor",
"reviewer", "status" or "approved" value, because the server decides those.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ai_ops_hub.analysis.schemas import FieldName, StoredAnalysis
from ai_ops_hub.cases.schemas import CATEGORY_TARGET, CaseSummary, ReviewStatus
from ai_ops_hub.schemas import MessageDetail, StoredMessage

T = TypeVar("T")


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# FieldName values plus "category".
CorrectionTarget = StrEnum(
    "CorrectionTarget", {n.name: n.value for n in FieldName} | {"CATEGORY": CATEGORY_TARGET}
)


class TextImportRequest(StrictRequest):
    text: str = Field(
        min_length=1,
        description="A pasted email. Optional 'From:' / 'Subject:' / 'Message-ID:' header lines, "
        "then a blank line, then the body.",
    )


class EditCaseRequest(StrictRequest):
    expected_version: int = Field(ge=1, description="The case version this edit is based on (optimistic lock).")
    corrections: dict[CorrectionTarget, list[str] | None] = Field(
        default_factory=dict,
        description="target -> the complete list of correct values; null reverts the target to the "
        "analyzer's value. 'category' takes exactly one category. Re-entering the analyzer's own "
        "value CONFIRMS it (clears an 'ambiguous' flag).",
    )
    note: str | None = Field(default=None, min_length=1, max_length=2000, description="A review note.")


class ApproveRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, min_length=1, max_length=2000)
    override_reason: str | None = Field(
        default=None,
        min_length=10,
        max_length=2000,
        description="Needed only when the case data still has problems: why a human approves anyway. "
        "It is stored with the decision and in the audit trail.",
    )


class ReasonRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=2000)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class MessageListItem(StoredMessage):
    case_id: int | None = None
    case_status: ReviewStatus | None = None


class MessageView(BaseModel):
    message: MessageDetail
    case_id: int | None = None
    case_status: ReviewStatus | None = None
    analysis_count: int = 0


class AnalyzeResponse(BaseModel):
    analysis_created: bool
    case_created: bool
    analysis: StoredAnalysis
    case: CaseSummary


class Health(BaseModel):
    status: str = "ok"
    database: str = "ok"
    auth: str = "none (local demo; reviewer is the fixed label 'local_demo_user')"
