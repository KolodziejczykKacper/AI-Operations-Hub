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
