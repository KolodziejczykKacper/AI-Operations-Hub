"""One error format for the whole API:

    {"error": {"code": "<machine_code>", "message": "<human text>", "details": {...}}}

Domain errors map to HTTP statuses in ONE table; unexpected errors never leak internals.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_ops_hub import errors as E

log = logging.getLogger("ai_ops_hub.api")


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict | list | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# exception type -> (HTTP status, machine code). Subclasses are matched via the MRO.
DOMAIN_ERRORS: dict[type[Exception], tuple[int, str]] = {
    E.EmptyInputError: (422, "empty_input"),
    E.MalformedMessageError: (422, "malformed_message"),
    E.InputTooLargeError: (413, "input_too_large"),
    E.MessageNotFoundError: (404, "message_not_found"),
    E.CaseNotFoundError: (404, "case_not_found"),
    E.CaseNotAvailableError: (409, "case_not_available"),
    E.ConflictNotAnalyzableError: (409, "conflict_not_analyzable"),
    E.CaseNotEditableError: (409, "case_not_editable"),
    E.InvalidTransitionError: (409, "invalid_transition"),
    E.VersionConflictError: (409, "version_conflict"),
    E.ApprovalBlockedError: (422, "approval_blocked"),
    E.InvalidCorrectionError: (422, "invalid_correction"),
    E.AnalyzerContractError: (500, "analyzer_contract_violation"),
    E.NoApprovedCasesError: (409, "no_approved_cases"),
    E.ExportTooLargeError: (422, "export_too_large"),
    E.InvalidExportFilterError: (422, "invalid_export_filter"),
    E.ExportDatabaseError: (503, "export_database_error"),
    E.ExportGenerationError: (500, "export_generation_failed"),
}


def error_response(status: int, code: str, message: str, details=None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def _domain_handler(_request: Request, exc: Exception) -> JSONResponse:
    status, code = next(DOMAIN_ERRORS[c] for c in type(exc).__mro__ if c in DOMAIN_ERRORS)
    details = None
    if isinstance(exc, E.VersionConflictError):
        details = {"current_version": exc.current_version}
    elif isinstance(exc, E.ExportTooLargeError):
        details = {"eligible": exc.eligible, "limit": exc.limit}
    elif isinstance(exc, (E.ApprovalBlockedError, E.InvalidCorrectionError)):
        details = {"problems": exc.problems}
    message = str(exc) if status < 500 else "the server could not complete the request"
    if status >= 500:
        log.error("domain error %s: %s", code, exc)
    return error_response(status, code, message, details)


def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Only loc/msg/type: the raw "input" could echo large or sensitive payloads.
    problems = [{"loc": list(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]
    if any(p["type"] == "json_invalid" for p in problems):
        return error_response(400, "invalid_json", "the request body is not valid JSON", {"problems": problems})
    return error_response(422, "validation_error", "the request is invalid", {"problems": problems})


def _http_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    codes = {404: "not_found", 405: "method_not_allowed", 415: "unsupported_media_type"}
    return error_response(exc.status_code, codes.get(exc.status_code, f"http_{exc.status_code}"), str(exc.detail))


def _unexpected_handler(_request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error", exc_info=exc)
    return error_response(500, "internal_error", "internal server error")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(E.AIOpsError, _domain_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    app.add_exception_handler(Exception, _unexpected_handler)
