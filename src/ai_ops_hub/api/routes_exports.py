"""Excel export endpoints. Read-only: exporting never changes a case.

The route only parses the query and shapes the HTTP response; selection and workbook
generation live in ``ExportService``.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Response

from ai_ops_hub.api.deps import ServicesDep
from ai_ops_hub.api.errors import ErrorResponse
from ai_ops_hub.export.models import ExportFilter, ExportSummary
from ai_ops_hub.export.workbook import XLSX_MIME

router = APIRouter(prefix="/exports", tags=["exports"])

_ERRORS = {
    409: {"model": ErrorResponse, "description": "no_approved_cases: nothing to export"},
    422: {"model": ErrorResponse, "description": "invalid filter, or export_too_large (narrow the date range)"},
    503: {"model": ErrorResponse, "description": "export_database_error"},
}

_DESC = (
    "`approved_from` / `approved_to`: optional approval dates (UTC calendar days, inclusive)."
)


@router.get(
    "/approved-cases/summary",
    response_model=ExportSummary,
    summary="How many approved cases an export would contain right now",
    description="Read-only. " + _DESC,
    responses={k: v for k, v in _ERRORS.items() if k != 409},
)
def export_summary(
    services: ServicesDep, approved_from: date | None = None, approved_to: date | None = None
) -> ExportSummary:
    return services.exports.summary(ExportFilter(approved_from=approved_from, approved_to=approved_to))


@router.get(
    "/approved-cases.xlsx",
    summary="Download an Excel workbook of the approved cases",
    description="Only cases whose current status is `approved`; values are the human-approved data "
    "(corrections included). The server generating the file is NOT the same as the file being "
    "downloaded: no 'downloaded' event is recorded. No case is changed. " + _DESC,
    responses={200: {"content": {XLSX_MIME: {}}, "description": "the .xlsx file"}} | _ERRORS,
    response_class=Response,
)
def export_approved_cases(
    services: ServicesDep, approved_from: date | None = None, approved_to: date | None = None
) -> Response:
    result = services.exports.export(ExportFilter(approved_from=approved_from, approved_to=approved_to))
    return Response(
        content=result.content,
        media_type=XLSX_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Export-Case-Count": str(result.case_count),
            "X-Export-Skipped-Count": str(result.skipped_count),
        },
    )
