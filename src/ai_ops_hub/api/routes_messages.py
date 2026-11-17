"""Message endpoints: import, list, inspect, analyze, audit."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from ai_ops_hub.analysis.schemas import StoredAnalysis
from ai_ops_hub.api.deps import IdPath, ServicesDep
from ai_ops_hub.api.errors import ErrorResponse
from ai_ops_hub.api.schemas import (
    AnalyzeResponse,
    MessageListItem,
    MessageView,
    Page,
    TextImportRequest,
)
from ai_ops_hub.errors import MessageNotFoundError
from ai_ops_hub.parsing import parse_eml, parse_pasted_text
from ai_ops_hub.schemas import AuditEntry, DedupDecision, ImportResult

router = APIRouter(prefix="/messages", tags=["messages"])

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "no such message"}}
_STATUS_BY_DECISION = {
    DedupDecision.NEW: 201,
    DedupDecision.DUPLICATE: 200,
    DedupDecision.CONFLICT: 409,
}
_IMPORT_RESPONSES = {
    201: {"description": "new message stored"},
    200: {"description": "identical message already stored (duplicate); nothing new stored"},
    409: {
        "description": "same Message-ID but different content: kept as a separate record with "
        "import_status 'conflict'. The body is the usual import result, not an error envelope.",
        "model": ImportResult,
    },
    413: {"model": ErrorResponse, "description": "body larger than the configured limit"},
    415: {"model": ErrorResponse, "description": "wrong Content-Type"},
    422: {"model": ErrorResponse, "description": "empty or malformed email"},
}
_EML_TYPES = ("message/rfc822", "application/octet-stream")


def _require_message(services, message_id: int):
    detail = services.imports.get_message_detail(message_id)
    if detail is None:
        raise MessageNotFoundError(f"message {message_id} does not exist")
    return detail


@router.post(
    "/eml",
    response_model=ImportResult,
    status_code=201,
    summary="Import a .eml file",
    description="Send the raw bytes of the .eml file as the request body with Content-Type "
    "`message/rfc822` or `application/octet-stream` (in Swagger UI choose a file). Nothing is "
    "analyzed yet. Results: 201 new, 200 duplicate, 409 conflicting content (stored, never analyzed).",
    responses=_IMPORT_RESPONSES,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"message/rfc822": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def import_eml(request: Request, response: Response, services: ServicesDep) -> ImportResult:
    # Only non-"simple" content types are accepted: a web page in the user's browser can not
    # send them cross-site without a CORS preflight, which this API never grants (CSRF defence).
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in _EML_TYPES:
        raise HTTPException(415, f"Content-Type must be one of {_EML_TYPES}")
    data = await request.body()
    result = await run_in_threadpool(_import_eml, services, data)
    response.status_code = _STATUS_BY_DECISION[result.decision]
    return result


def _import_eml(services, data: bytes) -> ImportResult:
    return services.imports.import_message(parse_eml(data, services.settings))


@router.post(
    "/text",
    response_model=ImportResult,
    status_code=201,
    summary="Import pasted email text",
    responses=_IMPORT_RESPONSES,
)
def import_text(body: TextImportRequest, response: Response, services: ServicesDep) -> ImportResult:
    result = services.imports.import_message(parse_pasted_text(body.text, services.settings))
    response.status_code = _STATUS_BY_DECISION[result.decision]
    return result


@router.get("", response_model=Page[MessageListItem], summary="List messages (paginated)")
def list_messages(
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
) -> Page[MessageListItem]:
    rows = services.imports.list_messages(limit=limit, offset=offset)
    index = services.cases.case_index([m.id for m in rows])
    items = [
        MessageListItem(
            **m.model_dump(),
            case_id=index[m.id][0] if m.id in index else None,
            case_status=index[m.id][1] if m.id in index else None,
        )
        for m in rows
    ]
    return Page(items=items, total=services.imports.count_messages(), limit=limit, offset=offset)


@router.get("/{message_id}", response_model=MessageView, summary="Message details", responses=_NOT_FOUND)
def get_message(message_id: IdPath, services: ServicesDep) -> MessageView:
    detail = _require_message(services, message_id)
    case = services.cases.case_index([message_id]).get(message_id)
    return MessageView(
        message=detail,
        case_id=case[0] if case else None,
        case_status=case[1] if case else None,
        analysis_count=len(services.analysis.list_analyses(message_id)),
    )


@router.post(
    "/{message_id}/analyze",
    response_model=AnalyzeResponse,
    status_code=201,
    summary="Run the rule-based analysis and open the review case",
    description="Idempotent: analysing again with the same analyzer version returns the stored "
    "analysis (200). Messages imported as conflicts are refused (409) and never get a case.",
    responses={
        200: {"description": "analysis already existed"},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "conflicting import: not analyzable"},
    },
)
def analyze_message(message_id: IdPath, response: Response, services: ServicesDep) -> AnalyzeResponse:
    outcome = services.analysis.analyze_message(message_id)
    case, case_created = services.cases.ensure_case(message_id)
    response.status_code = 201 if outcome.created else 200
    return AnalyzeResponse(
        analysis_created=outcome.created,
        case_created=case_created,
        analysis=outcome.analysis,
        case=case,
    )


@router.get(
    "/{message_id}/analysis",
    response_model=StoredAnalysis,
    summary="Latest analysis (with its warnings)",
    responses={404: {"model": ErrorResponse, "description": "no such message, or not analyzed yet"}},
)
def latest_analysis(message_id: IdPath, services: ServicesDep) -> StoredAnalysis:
    _require_message(services, message_id)
    analyses = services.analysis.list_analyses(message_id)
    if not analyses:
        raise HTTPException(404, f"message {message_id} has not been analyzed yet")
    return analyses[-1]


@router.get(
    "/{message_id}/analyses",
    response_model=list[StoredAnalysis],
    summary="All analyses of a message, oldest first (earlier versions are never overwritten)",
    responses=_NOT_FOUND,
)
def all_analyses(message_id: IdPath, services: ServicesDep) -> list[StoredAnalysis]:
    _require_message(services, message_id)
    return services.analysis.list_analyses(message_id)


@router.get(
    "/{message_id}/audit",
    response_model=list[AuditEntry],
    summary="Audit history of a message (import, analysis and case events)",
    responses=_NOT_FOUND,
)
def message_audit(message_id: IdPath, services: ServicesDep) -> list[AuditEntry]:
    _require_message(services, message_id)
    return services.imports.audit_history(message_id)
