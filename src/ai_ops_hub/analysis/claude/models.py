"""HTTP-facing models for the optional Claude analysis (requests forbid unknown fields)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ai_ops_hub.analysis.schemas import StoredAnalysis

PRIVACY_WARNING = (
    "The subject and body of this email will be sent to Anthropic (an external service in the cloud) "
    "and processed under Anthropic's terms. Do not use real, personal or confidential emails for demos. "
    "The sender address, recipients and headers are NOT sent."
)
COST_DISCLAIMER = (
    "Costs are local estimates from a price table, not a guaranteed spending limit. "
    "The Anthropic Console is the source of truth: set a billing limit there as well."
)


class ClaudeConsent(BaseModel):
    """Explicit consent for ONE request. It must match what the preview showed."""

    model_config = ConfigDict(extra="forbid")

    message_id: int = Field(ge=1, description="The message the preview was for; must equal the message in the URL.")
    content_fingerprint: str = Field(
        min_length=8, max_length=128, description="The content fingerprint from the preview (the exact text you saw)."
    )
    confirm_send_to_anthropic: bool = Field(
        description="Must be true: you confirm the email content is transmitted to Anthropic."
    )
    model: str = Field(min_length=1, max_length=100, description="The model you saw in the preview.")
    accepted_max_cost_usd: float = Field(
        ge=0, le=1000, description="At least the worst-case cost estimate shown in the preview."
    )


class ClaudeStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    key_configured: bool
    model: str
    model_has_known_price: bool
    input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None
    prices_verified_on: str
    max_output_tokens: int
    max_input_chars: int
    max_cost_usd_per_request: float
    daily_budget_usd: float
    spent_last_24h_usd: float
    can_analyze: bool
    blocked_reason: str | None
    privacy_warning: str = PRIVACY_WARNING
    cost_disclaimer: str = COST_DISCLAIMER


class ClaudePreview(BaseModel):
    """What WOULD be sent and what it could cost. Computed locally: no network, no cost."""

    model_config = ConfigDict(frozen=True)

    message_id: int
    enabled: bool
    key_configured: bool
    model: str
    content_fingerprint: str
    will_send: tuple[str, ...] = ("subject", "body")
    not_sent: tuple[str, ...] = ("sender address", "recipients", "headers")
    subject_chars: int
    body_chars: int
    estimated_input_tokens: int
    max_output_tokens: int
    estimated_max_cost_usd: float
    already_analyzed: bool
    existing_analysis_id: int | None
    can_send: bool
    blocked_reason: str | None
    privacy_warning: str = PRIVACY_WARNING
    cost_disclaimer: str = COST_DISCLAIMER


class LlmUsage(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: int
    message_id: int
    analysis_id: int | None
    model: str
    status: str
    error_type: str | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_max_cost_usd: float
    actual_cost_usd: float | None
    latency_ms: int | None
    request_id: str | None
    values_proposed: int
    values_rejected: int


class ClaudeAnalyzeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    created: bool  # False: this message was already analysed with this model/prompt; nothing was sent
    analysis: StoredAnalysis
    usage: LlmUsage | None
    case_id: int | None  # the message's case, if any (it keeps its own analysis)
    note: str


class CreateCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: int = Field(ge=1, description="The analysis the new case will review (rule_based or llm).")
