"""Optional Claude analysis endpoints. Nothing here runs automatically: imports and the normal
analyze endpoint never call Claude. The paid action needs an explicit consent body."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ai_ops_hub.analysis.claude.models import (
    ClaudeAnalyzeResponse,
    ClaudeConsent,
    ClaudePreview,
    ClaudeStatus,
    CreateCaseRequest,
    LlmUsage,
)
from ai_ops_hub.analysis.compare import AnalysisComparison
from ai_ops_hub.api.deps import IdPath, ServicesDep
from ai_ops_hub.api.errors import ErrorResponse
from ai_ops_hub.cases.schemas import CaseSummary

router = APIRouter(tags=["claude"])

_E = {
    409: {"model": ErrorResponse, "description": "claude_disabled / claude_not_configured / conflicting import"},
    422: {"model": ErrorResponse, "description": "claude_consent_required / claude_cost_guard"},
    413: {"model": ErrorResponse, "description": "claude_input_too_large"},
    429: {"model": ErrorResponse, "description": "claude_rate_limited"},
    502: {"model": ErrorResponse, "description": "claude_auth_failed / claude_bad_response / claude_provider_error"},
    504: {"model": ErrorResponse, "description": "claude_timeout"},
}


@router.get("/claude/status", response_model=ClaudeStatus,
            summary="Is optional Claude analysis enabled, which model, which limits (never returns the key)")
def claude_status(services: ServicesDep) -> ClaudeStatus:
    return services.claude.status()


@router.get("/messages/{message_id}/claude/preview", response_model=ClaudePreview,
            summary="What would be sent to Anthropic and the worst-case cost (local only: no network, no cost)")
def claude_preview(message_id: IdPath, services: ServicesDep) -> ClaudePreview:
    return services.claude.preview(message_id)


@router.post("/messages/{message_id}/claude/analyze", response_model=ClaudeAnalyzeResponse, status_code=201,
             summary="PAID, explicit: send this email to the Claude API and store the result as a separate analysis",
             description="Needs a consent body that matches the preview. Never creates or changes a case. "
             "A failed request stores no analysis. The SDK is configured without automatic retries.",
             responses=_E)
def claude_analyze(message_id: IdPath, consent: ClaudeConsent, services: ServicesDep) -> ClaudeAnalyzeResponse:
    return services.claude.analyze(message_id, consent)


@router.get("/messages/{message_id}/claude/usage", response_model=list[LlmUsage],
            summary="Recorded Claude requests for this message (tokens, cost estimate, latency; never content)")
def claude_usage(message_id: IdPath, services: ServicesDep) -> list[LlmUsage]:
    return services.claude.usage_for_message(message_id)


@router.get("/messages/{message_id}/compare", response_model=AnalysisComparison,
            summary="Read-only comparison of the rule-based and the Claude analysis of one message",
            description="Defaults: latest rule_based (a) against latest llm (b). Changes nothing.",
            responses={404: {"model": ErrorResponse, "description": "message or analysis not found"}})
def compare_analyses(
    message_id: IdPath,
    services: ServicesDep,
    analysis_a: Annotated[int | None, Query(ge=1)] = None,
    analysis_b: Annotated[int | None, Query(ge=1)] = None,
) -> AnalysisComparison:
    return services.claude.compare(message_id, analysis_a, analysis_b)


@router.post("/messages/{message_id}/case", response_model=CaseSummary, status_code=201,
             summary="Create the review case from an explicitly chosen analysis (rule_based or llm)",
             description="Only for a message WITHOUT a case. An existing case keeps its analysis (409).",
             responses={409: {"model": ErrorResponse, "description": "the message already has a case"}})
def create_case_from_analysis(message_id: IdPath, body: CreateCaseRequest, services: ServicesDep) -> CaseSummary:
    case, created = services.cases.ensure_case(message_id, body.analysis_id)
    if not created:
        from ai_ops_hub.errors import InvalidTransitionError

        raise InvalidTransitionError(
            f"message {message_id} already has case {case.id}; it keeps its analysis (use the comparison)"
        )
    return case
