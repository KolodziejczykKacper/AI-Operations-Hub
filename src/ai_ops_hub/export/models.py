"""Plain data carried between the export service (what to export) and the workbook builder (how)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class ExportFilter(BaseModel):
    """Optional approval-date window (UTC calendar days, both ends inclusive)."""

    model_config = ConfigDict(frozen=True)

    approved_from: date | None = None
    approved_to: date | None = None


class ExportSummary(BaseModel):
    """What an export would contain right now (read-only; nothing is generated)."""

    model_config = ConfigDict(frozen=True)

    eligible_count: int
    skipped_count: int  # approved cases left out because their approval record is inconsistent
    max_export_cases: int
    exceeds_limit: bool
    approved_from: date | None = None
    approved_to: date | None = None


@dataclass(frozen=True)
class ExportValue:
    value: str
    confirmed: bool  # False: the analyzer was unsure (ambiguous / invalid) and no human confirmed it
    status: str  # valid | ambiguous | invalid


@dataclass(frozen=True)
class ExportRow:
    case_id: int
    version: int
    category: str
    sender: str | None
    subject: str | None
    imported_at: datetime | None
    approved_at: datetime
    fields: dict[str, tuple[ExportValue, ...]]  # FieldName value -> values (absent = not stated)
    approval_note: str | None
    other_notes: tuple[str, ...]
    override_used: bool
    override_reason: str | None
    overridden_problems: tuple[str, ...]
    unconfirmed: tuple[str, ...]
    corrections: tuple[str, ...]
    analysis_method: str = "rule_based"  # how the analysis behind this case was produced
    analyzer_version: str = ""


@dataclass(frozen=True)
class ExportFile:
    content: bytes
    filename: str
    case_count: int
    skipped_count: int
    generated_at: datetime


@dataclass
class Collected:
    rows: list[ExportRow] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)  # case ids
