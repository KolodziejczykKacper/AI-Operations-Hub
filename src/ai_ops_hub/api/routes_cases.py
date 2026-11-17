"""Case endpoints: list, inspect, correct, approve, reject, reopen, history, audit.

Every mutation names the ``expected_version`` it is based on (optimistic locking) and is
performed by the fixed reviewer label ``local_demo_user``; the client cannot choose it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ai_ops_hub.api.deps import IdPath, ServicesDep
from ai_ops_hub.api.errors import ErrorResponse
from ai_ops_hub.api.schemas import ApproveRequest, EditCaseRequest, Page, ReasonRequest
from ai_ops_hub.cases.schemas import CaseDetail, CaseHistory, CaseSummary, ReviewStatus
from ai_ops_hub.schemas import AuditEntry

router = APIRouter(prefix="/cases", tags=["cases"])

_E404 = {404: {"model": ErrorResponse, "description": "no such case"}}
_E409 = {
    409: {
        "model": ErrorResponse,
        "description": "stale version (version_conflict) or illegal state transition (invalid_transition)",
    }
}


@router.get("", response_model=Page[CaseSummary], summary="List cases (paginated, optional status filter)")
def list_cases(
    services: ServicesDep,
    status: ReviewStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
) -> Page[CaseSummary]:
    items, total = services.cases.list_cases(status, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/{case_id}",
    response_model=CaseDetail,
    summary="Case with the original email, effective fields (+ source spans), problems and allowed actions",
    responses=_E404,
)
def get_case(case_id: IdPath, services: ServicesDep) -> CaseDetail:
    return services.cases.get_case(case_id)


@router.patch(
    "/{case_id}",
    response_model=CaseDetail,
    summary="Correct extracted values and/or add a review note",
    description="Corrections are stored separately and append-only; the original email and the "
    "analyzer output are never changed. The case stays open: needs_review <-> ready_for_review is "
    "recomputed from the corrected data. It is never approved by an edit.",
    responses=_E404 | _E409
    | {422: {"model": ErrorResponse, "description": "invalid value, or nothing to change"}},
)
def edit_case(case_id: IdPath, body: EditCaseRequest, services: ServicesDep) -> CaseDetail:
    corrections = {target.value: values for target, values in body.corrections.items()}
    return services.cases.edit_case(case_id, body.expected_version, corrections, body.note)


@router.post(
    "/{case_id}/approve",
    response_model=CaseDetail,
    summary="Approve (explicit human decision)",
    description="Validates the EFFECTIVE data (after corrections). If problems remain, answer 422 "
    "`approval_blocked` listing them, unless `override_reason` documents why a human approves anyway. "
    "Approval triggers no external action: nothing is sent or executed.",
    responses=_E404 | _E409
    | {422: {"model": ErrorResponse, "description": "approval_blocked: problems need a fix or an override"}},
)
def approve_case(case_id: IdPath, body: ApproveRequest, services: ServicesDep) -> CaseDetail:
    return services.cases.approve(case_id, body.expected_version, body.note, body.override_reason)


@router.post(
    "/{case_id}/reject",
    response_model=CaseDetail,
    summary="Reject (explicit human decision)",
    responses=_E404 | _E409,
)
def reject_case(case_id: IdPath, body: ReasonRequest, services: ServicesDep) -> CaseDetail:
    return services.cases.reject(case_id, body.expected_version, body.reason)


@router.post(
    "/{case_id}/reopen",
    response_model=CaseDetail,
    summary="Reopen an approved or rejected case",
    responses=_E404 | _E409,
)
def reopen_case(case_id: IdPath, body: ReasonRequest, services: ServicesDep) -> CaseDetail:
    return services.cases.reopen(case_id, body.expected_version, body.reason)


@router.get(
    "/{case_id}/history",
    response_model=CaseHistory,
    summary="Every human correction and review decision, oldest first",
    responses=_E404,
)
def case_history(case_id: IdPath, services: ServicesDep) -> CaseHistory:
    return services.cases.history(case_id)


@router.get(
    "/{case_id}/audit",
    response_model=list[AuditEntry],
    summary="Audit trail of the case's message (import, analysis, every case event)",
    responses=_E404,
)
def case_audit(case_id: IdPath, services: ServicesDep) -> list[AuditEntry]:
    return services.cases.audit(case_id)
