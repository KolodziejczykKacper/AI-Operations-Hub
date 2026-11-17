"""Domain schemas for message analysis (analyzer-agnostic).

Nothing here is specific to the rule-based analyzer: a future LLM adapter
returns the same ``AnalysisResult``. There is deliberately NO approval field
and NO calibrated-probability field: results are suggestions for human review.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_ops_hub.schemas import AnalysisMethod, ExtractedValue, SourceSpan


class Category(StrEnum):
    ORDER = "order"
    COMPLAINT = "complaint"
    INQUIRY = "inquiry"
    DOCUMENT_REQUEST = "document_request"
    OTHER = "other"


class FieldName(StrEnum):
    CONTACT_NAME = "contact_name"
    ORGANIZATION = "organization"
    REFERENCE = "reference"
    PRODUCT = "product"
    QUANTITY = "quantity"
    AMOUNT = "amount"
    DEADLINE = "deadline"
    REQUESTED_DATE = "requested_date"
    EMAIL = "email"
    PHONE = "phone"


# Fields whose ambiguity or invalidity should send the message to a human.
MATERIAL_FIELDS = frozenset(
    {FieldName.AMOUNT, FieldName.QUANTITY, FieldName.DEADLINE, FieldName.REQUESTED_DATE}
)


class FieldStatus(StrEnum):
    VALID = "valid"
    AMBIGUOUS = "ambiguous"  # extracted, but meaning not fully determined by the text
    INVALID = "invalid"  # text found, but not a valid value (e.g. 31 November)


class WarningCode(StrEnum):
    CURRENCY_NOT_STATED = "currency_not_stated"
    CURRENCY_AMBIGUOUS = "currency_ambiguous"
    NUMBER_FORMAT_AMBIGUOUS = "number_format_ambiguous"
    NUMBER_UNPARSEABLE = "number_unparseable"
    YEAR_NOT_STATED = "year_not_stated"
    DAY_NOT_STATED = "day_not_stated"
    TWO_DIGIT_YEAR = "two_digit_year"
    DATE_FORMAT_AMBIGUOUS = "date_format_ambiguous"
    DATE_INVALID = "date_invalid"
    DATE_WITHOUT_ROLE = "date_without_role"
    DATE_RANGE = "date_range"  # a window of dates, not a single deadline
    AVAILABILITY_DATE = "availability_date"  # return/closure date, not a business deadline
    UNLABELLED_CODE = "unlabelled_code"  # code-like token without reference context
    RELATIVE_DATE_NOT_RESOLVED = "relative_date_not_resolved"
    APPROXIMATE_QUANTITY = "approximate_quantity"
    QUANTITY_RANGE = "quantity_range"
    MULTIPLE_QUANTITIES = "multiple_quantities"
    PARTIAL_NAME = "partial_name"
    MULTIPLE_NAMES = "multiple_names"
    PHONE_LENGTH_INVALID = "phone_length_invalid"
    NO_COUNTRY_CODE = "no_country_code"
    EMAIL_INVALID = "email_invalid"
    POSSIBLE_PROMPT_INJECTION = "possible_prompt_injection"


class ReviewCode(StrEnum):
    NO_CATEGORY_MATCH = "no_category_match"
    AMBIGUOUS_CATEGORY = "ambiguous_category"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    FIELD_NEEDS_REVIEW = "field_needs_review"
    UNRESOLVED_RELATIVE_DATE = "unresolved_relative_date"
    UNRESOLVED_DATE_RANGE = "unresolved_date_range"
    POSSIBLE_PROMPT_INJECTION = "possible_prompt_injection"


class AnalysisInput(BaseModel):
    """What an analyzer sees. Spans in the result refer to these exact strings."""

    model_config = ConfigDict(frozen=True)

    subject: str | None = None
    body_text: str
    from_addr: str | None = None


class AnalysisWarning(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: WarningCode
    message: str
    span: SourceSpan | None = None
    field: FieldName | None = None


class AnalyzedField(BaseModel):
    """One extracted value: normalized value + original span (``value``),
    plus how far it can be trusted (``status``) and why (``warnings``)."""

    model_config = ConfigDict(frozen=True)

    name: FieldName
    value: ExtractedValue
    status: FieldStatus = FieldStatus.VALID
    warnings: tuple[AnalysisWarning, ...] = ()


class CategoryIndicator(BaseModel):
    """A phrase that contributed to a category's heuristic score.

    ``negated`` indicators were found but are preceded by a negation ("this is
    not a complaint"); they carry weight 0 and are kept for transparency."""

    model_config = ConfigDict(frozen=True)

    category: Category
    weight: int
    span: SourceSpan
    negated: bool = False


class ReviewReason(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: ReviewCode
    message: str
    field: FieldName | None = None


class AnalysisResult(BaseModel):
    """Outcome of analysing one message.

    ``heuristic_scores`` are raw rule-hit sums. They are NOT probabilities and
    NOT calibrated confidence; compare them only with each other.
    """

    model_config = ConfigDict(frozen=True)

    analyzer_name: str
    analyzer_version: str
    analysis_method: AnalysisMethod
    category: Category
    heuristic_scores: dict[Category, int] = Field(default_factory=dict)
    indicators: tuple[CategoryIndicator, ...] = ()
    category_alternatives: tuple[Category, ...] = ()
    fields: tuple[AnalyzedField, ...] = ()
    warnings: tuple[AnalysisWarning, ...] = ()
    review_reasons: tuple[ReviewReason, ...] = ()
    requires_review: bool = False

    @model_validator(mode="after")
    def _review_flag_matches_reasons(self) -> "AnalysisResult":
        if self.requires_review != bool(self.review_reasons):
            raise ValueError("requires_review must be true exactly when review_reasons is non-empty")
        return self

    def fields_named(self, name: FieldName) -> list[AnalyzedField]:
        return [f for f in self.fields if f.name is name]

    def verify_provenance(self, body_text: str, subject: str | None = None) -> bool:
        """Every stored span must still match the source text exactly."""
        spans = [f.value.span for f in self.fields]
        spans += [i.span for i in self.indicators]
        spans += [w.span for w in self.warnings if w.span is not None]
        return all(s.matches(body_text, subject) for s in spans)


class StoredAnalysis(BaseModel):
    """Read model of a persisted analysis (one row per message + analyzer version)."""

    model_config = ConfigDict(frozen=True)

    id: int
    message_id: int
    analyzer_name: str
    analyzer_version: str
    analysis_method: AnalysisMethod
    category: Category
    requires_review: bool
    source_fingerprint: str
    created_at: datetime
    result: AnalysisResult


class AnalysisOutcome(BaseModel):
    """``created`` is False when an analysis by this analyzer version already existed."""

    model_config = ConfigDict(frozen=True)

    created: bool
    analysis: StoredAnalysis
