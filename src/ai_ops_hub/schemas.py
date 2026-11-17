"""Domain schemas for Stage 1.

Only data shapes and invariants live here. No I/O, no database, no analysis.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceKind(StrEnum):
    EML = "eml"
    PASTED_TEXT = "pasted_text"


class ParsedMessage(BaseModel):
    """A message after parsing. ``body_text`` is the canonical text that all
    source spans refer to (character offsets into this exact string)."""

    model_config = ConfigDict(frozen=True)

    source_kind: SourceKind
    message_id: str | None = None  # normalized, used for deduplication
    message_id_raw: str | None = None  # exactly as found in the source
    subject: str | None = None
    from_addr: str | None = None
    to_addrs: tuple[str, ...] = ()
    date_raw: str | None = None
    date: datetime | None = None
    body_text: str
    raw_sha256: str
    missing_headers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


# --- Provenance -------------------------------------------------------------


class AnalysisMethod(StrEnum):
    """How a value was produced. The offline analyzer is ALWAYS ``rule_based``
    and must never be presented as AI inference."""

    RULE_BASED = "rule_based"
    LLM = "llm"
    HUMAN = "human"


class ValueKind(StrEnum):
    AMOUNT = "amount"
    DATE = "date"
    DEADLINE = "deadline"
    PARTY = "party"
    REFERENCE = "reference"
    OTHER = "other"


class SourceSpan(BaseModel):
    """Half-open character range [start, end) in ``ParsedMessage.body_text``
    plus the exact text found there."""

    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str

    @model_validator(mode="after")
    def _check(self) -> "SourceSpan":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        if len(self.text) != self.end - self.start:
            raise ValueError("text length must equal end - start")
        return self

    def matches(self, body_text: str) -> bool:
        return body_text[self.start : self.end] == self.text


class ExtractedValue(BaseModel):
    """A normalized value that keeps the original text span it came from."""

    model_config = ConfigDict(frozen=True)

    kind: ValueKind
    normalized: str
    span: SourceSpan
    method: AnalysisMethod

    def verify_against(self, body_text: str) -> bool:
        """True when the stored span still matches the source body exactly."""
        return self.span.matches(body_text)


# --- Deduplication ----------------------------------------------------------


class DedupDecision(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"  # same key, identical content
    CONFLICT = "conflict"  # same key, DIFFERENT content - never discarded


class DedupResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: DedupDecision
    key: str
    fingerprint: str
    existing_fingerprint: str | None = None
    reason: str


# --- Stored messages and import results (Stage 2) ---------------------------


class ImportStatus(StrEnum):
    IMPORTED = "imported"  # canonical record for its dedup key
    CONFLICT = "conflict"  # same key as a canonical record, different content


class StoredMessage(BaseModel):
    """Read model of a persisted message (no database types leak out)."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: int
    dedup_key: str
    content_fingerprint: str
    message_id: str | None
    subject: str | None
    from_addr: str | None
    source_kind: SourceKind
    import_status: ImportStatus  # dedup/import state, NOT a case-handling status
    imported_at: datetime
    conflicts_with_id: int | None


class ImportResult(BaseModel):
    """Outcome of importing one parsed message.

    ``message`` is the record the caller should use: the new record for NEW and
    CONFLICT, the already-stored record for DUPLICATE. For CONFLICT,
    ``existing_message`` is the untouched canonical record.
    """

    model_config = ConfigDict(frozen=True)

    decision: DedupDecision
    message: StoredMessage
    existing_message: StoredMessage | None = None
    reason: str


class AuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: int
    message_id: int
    event_type: AuditEventType
    actor: Actor
    timestamp: datetime
    details: dict[str, Any]


# --- Cases and audit (schemas only; persistence arrives in a later stage) ---


class CaseStatus(StrEnum):
    NEW = "new"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class Actor(StrEnum):
    HUMAN = "human"
    SYSTEM = "system"
    RULE_BASED_ANALYZER = "rule_based_analyzer"


class AuditEventType(StrEnum):
    MESSAGE_IMPORTED = "message_imported"
    CASE_CREATED = "case_created"
    CASE_UPDATED = "case_updated"
    STATUS_CHANGED = "status_changed"
    DUPLICATE_DETECTED = "duplicate_detected"
    CONFLICT_DETECTED = "conflict_detected"
    CASE_APPROVED = "case_approved"
    CASE_REJECTED = "case_rejected"
    # Deliberately no "email_sent": the system never sends email.


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    event_type: AuditEventType
    actor: Actor
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _approval_is_human_only(self) -> "AuditEvent":
        if self.event_type is AuditEventType.CASE_APPROVED and self.actor is not Actor.HUMAN:
            raise ValueError("cases can only be approved by a human actor")
        return self
