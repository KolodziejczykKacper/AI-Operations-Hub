"""SQLModel table definitions.

These are persistence models only. Business rules live in the service layer and
the domain schemas in ``ai_ops_hub.schemas``. Integrity rules that must hold no
matter which code writes (uniqueness, links, import-status consistency) are enforced
here by the database itself.

Note: no ``from __future__ import annotations`` on purpose - SQLModel resolves
field types at class creation.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    JSON,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


class UTCDateTime(TypeDecorator):
    """Stores timezone-aware datetimes as naive UTC; always returns aware UTC.

    Naive datetimes are rejected so a local-time value can never be stored by
    accident.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected; use timezone-aware UTC")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


class MessageRecord(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (
        # Identical content can exist once per key (identical re-imports are duplicates).
        UniqueConstraint("dedup_key", "content_fingerprint", name="uq_messages_key_fingerprint"),
        # At most ONE canonical record per key; conflicting variants are exempt.
        Index(
            "uq_messages_canonical_key",
            "dedup_key",
            unique=True,
            sqlite_where=text("import_status != 'conflict'"),
        ),
        CheckConstraint("import_status IN ('imported', 'conflict')", name="ck_messages_import_status"),
        # A row is a conflict variant if and only if it points at its canonical record.
        CheckConstraint(
            "(import_status = 'conflict') = (conflicts_with_id IS NOT NULL)",
            name="ck_messages_conflict_link",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)

    message_id: str | None = Field(default=None)  # normalized Message-ID
    message_id_raw: str | None = Field(default=None)
    dedup_key: str = Field(index=True)
    content_fingerprint: str

    source_kind: str
    subject: str | None = Field(default=None)
    from_addr: str | None = Field(default=None)
    to_addrs: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    date_raw: str | None = Field(default=None)
    sent_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime, nullable=True))
    body_text: str
    raw_sha256: str
    missing_headers: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    warnings: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))

    import_status: str  # dedup/import state; case-handling status will live on a separate case entity
    conflicts_with_id: int | None = Field(default=None, foreign_key="messages.id")
    imported_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))


class AnalysisRecord(SQLModel, table=True):
    """One analysis of one message by one analyzer version.

    Deliberately separate from ``messages`` so that ``import_status`` (dedup
    state) and any future case-handling status stay independent of analysis.
    A new analyzer version adds a new row; earlier rows are kept as history.
    """

    __tablename__ = "analyses"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "analyzer_name", "analyzer_version",
            name="uq_analyses_message_analyzer_version",
        ),
        CheckConstraint("analysis_method IN ('rule_based', 'llm')", name="ck_analyses_method"),
        CheckConstraint(
            "category IN ('order', 'complaint', 'inquiry', 'document_request', 'other')",
            name="ck_analyses_category",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="messages.id", index=True)
    analyzer_name: str
    analyzer_version: str
    analysis_method: str
    category: str
    requires_review: bool
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    source_fingerprint: str  # messages.content_fingerprint at analysis time
    created_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))


# Defence in depth: the service refuses to analyze conflicting imports, and the
# database refuses the row too. Created together with the table, so existing
# databases simply gain the new table and trigger (nothing is altered or dropped).
event.listen(
    AnalysisRecord.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER IF NOT EXISTS trg_analyses_no_conflict_insert
        BEFORE INSERT ON analyses
        WHEN (SELECT import_status FROM messages WHERE id = NEW.message_id) = 'conflict'
        BEGIN
            SELECT RAISE(ABORT, 'messages imported as conflicts must not be analyzed');
        END
        """
    ).execute_if(dialect="sqlite"),
)


class AuditEventRecord(SQLModel, table=True):
    __tablename__ = "audit_events"

    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="messages.id", index=True)
    event_type: str
    actor: str
    timestamp: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))
    details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


# --- Cases and human review (Stage 4; additive: new tables only) --------------


class CaseRecord(SQLModel, table=True):
    """The human-review workflow state of ONE message.

    Kept apart from ``messages`` (import/dedup state) and ``analyses`` (what the
    analyzer said). ``version`` is the optimistic-lock counter: every change to a
    case bumps it by exactly one.
    """

    __tablename__ = "cases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('needs_review', 'ready_for_review', 'approved', 'rejected')",
            name="ck_cases_status",
        ),
        CheckConstraint("version >= 1", name="ck_cases_version"),
        # decided_at is set exactly while the case is closed (approved / rejected).
        CheckConstraint(
            "(status IN ('approved', 'rejected')) = (decided_at IS NOT NULL)",
            name="ck_cases_decided_at",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="messages.id", unique=True)
    analysis_id: int = Field(foreign_key="analyses.id")  # the analysis this case reviews
    status: str
    version: int = Field(default=1)
    created_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))
    updated_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))
    decided_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime, nullable=True))


class CaseCorrectionRecord(SQLModel, table=True):
    """One human correction of one field (or of the category). Append-only.

    The latest row per (case, target) decides the effective value; earlier rows
    and the analyzer output stay untouched. ``action='revert'`` goes back to the
    analyzer's value. ``values`` is the full list of values that replaces the
    analyzer's values for that field (an empty list means "no such value").
    """

    __tablename__ = "case_corrections"
    __table_args__ = (
        CheckConstraint("action IN ('set', 'revert')", name="ck_case_corrections_action"),
    )

    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="cases.id", index=True)
    target: str  # a FieldName value or 'category'
    action: str
    values: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    previous_values: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    version_after: int
    reviewer: str
    created_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))


class CaseReviewRecord(SQLModel, table=True):
    """A review note or decision (approve / reject / reopen). Append-only.

    ``snapshot`` is the effective data at the moment of the decision, so it is
    always possible to see exactly what was approved.
    """

    __tablename__ = "case_reviews"
    __table_args__ = (
        CheckConstraint("action IN ('note', 'approve', 'reject', 'reopen')", name="ck_case_reviews_action"),
    )

    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="cases.id", index=True)
    action: str
    from_status: str
    to_status: str
    note: str | None = Field(default=None)
    override_reason: str | None = Field(default=None)
    overridden_problems: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    version_after: int
    reviewer: str
    created_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))


_CASE_TRIGGERS = (
    # A conflicting import never becomes a case (the service refuses first).
    """
    CREATE TRIGGER IF NOT EXISTS trg_cases_no_conflict_insert
    BEFORE INSERT ON cases
    WHEN (SELECT import_status FROM messages WHERE id = NEW.message_id) = 'conflict'
    BEGIN
        SELECT RAISE(ABORT, 'messages imported as conflicts must not become cases');
    END
    """,
    # A case is always born open: it can never be created already approved or rejected.
    """
    CREATE TRIGGER IF NOT EXISTS trg_cases_born_open
    BEFORE INSERT ON cases
    WHEN NEW.status NOT IN ('needs_review', 'ready_for_review')
    BEGIN
        SELECT RAISE(ABORT, 'a new case must start open');
    END
    """,
    # approved / rejected only together with a matching human decision record.
    """
    CREATE TRIGGER IF NOT EXISTS trg_cases_decision_needs_record
    BEFORE UPDATE OF status ON cases
    WHEN NEW.status != OLD.status AND NEW.status IN ('approved', 'rejected')
     AND NOT EXISTS (
        SELECT 1 FROM case_reviews
        WHERE case_id = NEW.id AND version_after = NEW.version
          AND action = CASE NEW.status WHEN 'approved' THEN 'approve' ELSE 'reject' END
     )
    BEGIN
        SELECT RAISE(ABORT, 'approve/reject needs a recorded human decision');
    END
    """,
    # approved <-> rejected directly is not a legal transition (reopen first).
    """
    CREATE TRIGGER IF NOT EXISTS trg_cases_no_direct_flip
    BEFORE UPDATE OF status ON cases
    WHEN OLD.status IN ('approved', 'rejected') AND NEW.status IN ('approved', 'rejected')
     AND NEW.status != OLD.status
    BEGIN
        SELECT RAISE(ABORT, 'reopen the case before changing a decision');
    END
    """,
)
for _ddl in _CASE_TRIGGERS:
    event.listen(CaseRecord.__table__, "after_create", DDL(_ddl).execute_if(dialect="sqlite"))

# History is append-only: corrections and reviews can be neither edited nor deleted.
for _table in (CaseCorrectionRecord.__table__, CaseReviewRecord.__table__):
    for _op in ("UPDATE", "DELETE"):
        event.listen(
            _table,
            "after_create",
            DDL(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{_table.name}_no_{_op.lower()}
                BEFORE {_op} ON {_table.name}
                BEGIN
                    SELECT RAISE(ABORT, '{_table.name} is append-only');
                END
                """
            ).execute_if(dialect="sqlite"),
        )


# --- Optional LLM analysis usage (Stage 7; additive: one new table) ------------------------


class LlmCallRecord(SQLModel, table=True):
    """One paid (or attempted) Claude request. Stores NO prompt and NO model text.

    ``status`` is ``reserved`` while the request runs (its worst-case estimate already counts against the
    local daily budget), then ``ok`` or ``failed``.
    ``estimated_max_cost_usd`` is the local worst-case estimate shown before the request;
    ``actual_cost_usd`` is computed from the token usage the API returned (NULL if none).
    Neither is a billing record: the Anthropic Console is the source of truth.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (CheckConstraint("status IN ('reserved', 'ok', 'failed')", name="ck_llm_calls_status"),)

    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="messages.id", index=True)
    analysis_id: int | None = Field(default=None, foreign_key="analyses.id")
    model: str
    status: str
    error_type: str | None = Field(default=None)
    input_tokens: int | None = Field(default=None)
    output_tokens: int | None = Field(default=None)
    estimated_max_cost_usd: float
    actual_cost_usd: float | None = Field(default=None)
    latency_ms: int | None = Field(default=None)
    request_id: str | None = Field(default=None)
    values_proposed: int = Field(default=0)
    values_rejected: int = Field(default=0)
    created_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))
