"""The ONLY place where the UI talks to the backend: a small HTTP client for the FastAPI app.

* HTTP only: no database, no repositories, no business services.
* Base URL is configurable (default http://127.0.0.1:8000) and must be a loopback address
  unless ``allow_remote=True``; the client never talks to anything else. Proxy environment
  variables are ignored (``trust_env=False``) and redirects are not followed.
* Every call has a timeout. Mutating calls are never retried, so a flaky connection can not
  repeat an approval; a timeout on a mutation is reported as "outcome unknown".
* Failures become typed exceptions with messages that are safe to show to a user.
* The client contains no validation or state-transition logic: the server decides.
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import date
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx2 as httpx
from pydantic import BaseModel, ValidationError

from ai_ops_hub.ui import models as m

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=20.0, write=20.0, pool=3.0)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FALLBACK_FILENAME = "approved-cases.xlsx"

M = TypeVar("M", bound=BaseModel)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _safe_filename(disposition: str) -> str:
    """The file name the server suggested, reduced to a plain, safe .xlsx name."""
    match = re.search(r'filename="?([A-Za-z0-9._-]{1,100})"?', disposition)
    name = match.group(1) if match else FALLBACK_FILENAME
    return name if name.lower().endswith(".xlsx") and ".." not in name else FALLBACK_FILENAME


def _int_header(response: "httpx.Response", name: str) -> int:
    try:
        return max(0, int(response.headers.get(name, "0")))
    except ValueError:
        return 0


# --- errors --------------------------------------------------------------------------------


class ClientError(Exception):
    """Base class; ``str(error)`` is safe to show to the user."""


class BackendUnavailable(ClientError):
    """The API could not be reached (not running, refused, timed out)."""

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        # True when the request may have reached the server before the connection failed.
        self.outcome_unknown = outcome_unknown


class InvalidResponse(ClientError):
    """The API answered, but not with the expected JSON contract."""


class ApiError(ClientError):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class VersionConflict(ApiError):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(status, code, message, details)
        self.current_version: int | None = (details or {}).get("current_version") if isinstance(details, dict) else None


class _WithProblems(ApiError):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(status, code, message, details)
        raw = (details or {}).get("problems", []) if isinstance(details, dict) else []
        self.problems: list[dict] = [p for p in raw if isinstance(p, dict)]


class ApprovalBlocked(_WithProblems):
    """The server refuses the approval because the case data has problems."""


class InvalidCorrection(_WithProblems):
    """The server rejected a corrected value (``problems`` say which and why)."""


class ValidationFailed(_WithProblems):
    """The request was malformed (422 validation_error)."""


_ERROR_CLASSES: dict[str, type[ApiError]] = {
    "version_conflict": VersionConflict,
    "approval_blocked": ApprovalBlocked,
    "invalid_correction": InvalidCorrection,
    "validation_error": ValidationFailed,
}


# --- client ----------------------------------------------------------------------------------


def _check_base_url(url: str, allow_remote: bool) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not a usable API address: {url!r}")
    host = parts.hostname
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback and not allow_remote:
        raise ValueError(
            f"refusing to talk to {host!r}: this UI only connects to a local API (loopback address)"
        )
    return url.rstrip("/")


class ApiClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: httpx.Timeout | float | None = None,
        http: httpx.Client | None = None,
        allow_remote: bool = False,
    ) -> None:
        self.base_url = _check_base_url(base_url, allow_remote)
        if http is None:
            http = httpx.Client(
                base_url=self.base_url,
                timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            )
        self._http = http

    # -- low level ----------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
        ok_statuses: tuple[int, ...] = (),
    ) -> Any:
        response = self._send(
            method, path, params=params, json_body=json_body, content=content, content_type=content_type
        )
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise InvalidResponse(f"The API returned a non-JSON answer (HTTP {response.status_code}).") from exc

        if 200 <= response.status_code < 300 or response.status_code in ok_statuses:
            return data
        raise self._error(response.status_code, data)

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
    ) -> "httpx.Response":
        """One HTTP round trip with the timeout / unavailable handling; no status interpretation."""
        mutating = method.upper() != "GET"
        headers = {"Accept": accept}
        if content_type:
            headers["Content-Type"] = content_type
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = self._http.request(
                method, path, params=params or None, json=json_body, content=content, headers=headers
            )
        except httpx.ConnectError as exc:
            raise BackendUnavailable(
                f"Cannot connect to the API at {self.base_url}. Is the backend running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise BackendUnavailable(
                f"The API at {self.base_url} did not answer in time.",
                outcome_unknown=mutating,
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendUnavailable(
                f"Connection to the API failed ({type(exc).__name__}).", outcome_unknown=mutating
            ) from exc
        return response

    @staticmethod
    def _error(status: int, data: Any) -> ApiError:
        err = data.get("error") if isinstance(data, dict) else None
        if not isinstance(err, dict):
            return ApiError(status, "http_error", f"The API answered HTTP {status}.")
        code = str(err.get("code", "http_error"))
        message = str(err.get("message", f"HTTP {status}"))
        return _ERROR_CLASSES.get(code, ApiError)(status, code, message, err.get("details"))

    @staticmethod
    def _parse(model: type[M], data: Any) -> M:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise InvalidResponse(
                f"The API answer did not match the expected format ({model.__name__})."
            ) from exc

    # -- reads --------------------------------------------------------------------------------

    def health(self) -> m.Health:
        return self._parse(m.Health, self._request("GET", "/health"))

    def stats(self) -> m.Stats:
        return self._parse(m.Stats, self._request("GET", "/cases/stats"))

    def list_cases(
        self, *, status: str | None = None, q: str | None = None, limit: int = 15, offset: int = 0
    ) -> m.Page[m.CaseSummary]:
        data = self._request(
            "GET", "/cases", params={"status": status, "q": q or None, "limit": limit, "offset": offset}
        )
        return self._parse(m.Page[m.CaseSummary], data)

    def get_case(self, case_id: int) -> m.CaseDetail:
        return self._parse(m.CaseDetail, self._request("GET", f"/cases/{int(case_id)}"))

    def history(self, case_id: int) -> m.History:
        return self._parse(m.History, self._request("GET", f"/cases/{int(case_id)}/history"))

    def case_audit(self, case_id: int) -> list[m.AuditEntry]:
        data = self._request("GET", f"/cases/{int(case_id)}/audit")
        return [self._parse(m.AuditEntry, item) for item in self._as_list(data)]

    def list_messages(self, *, limit: int = 15, offset: int = 0) -> m.Page[m.MessageListItem]:
        data = self._request("GET", "/messages", params={"limit": limit, "offset": offset})
        return self._parse(m.Page[m.MessageListItem], data)

    def get_message(self, message_id: int) -> m.MessageView:
        return self._parse(m.MessageView, self._request("GET", f"/messages/{int(message_id)}"))

    def export_summary(
        self, *, approved_from: date | None = None, approved_to: date | None = None
    ) -> m.ExportSummary:
        data = self._request(
            "GET",
            "/exports/approved-cases/summary",
            params={"approved_from": _iso(approved_from), "approved_to": _iso(approved_to)},
        )
        return self._parse(m.ExportSummary, data)

    def export_approved_cases(
        self, *, approved_from: date | None = None, approved_to: date | None = None
    ) -> m.ExportFile:
        """Ask the server to generate the workbook (a read-only GET; never retried by the UI)."""
        response = self._send(
            "GET",
            "/exports/approved-cases.xlsx",
            params={"approved_from": _iso(approved_from), "approved_to": _iso(approved_to)},
            accept=XLSX_MIME,
        )
        if not 200 <= response.status_code < 300:
            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError):
                data = None
            raise self._error(response.status_code, data)
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        body = response.content
        if content_type != XLSX_MIME or not body.startswith(b"PK"):  # an .xlsx is a ZIP archive
            raise InvalidResponse("The API did not return an Excel file.")
        return m.ExportFile(
            content=body,
            filename=_safe_filename(response.headers.get("content-disposition", "")),
            case_count=_int_header(response, "x-export-case-count"),
            skipped_count=_int_header(response, "x-export-skipped-count"),
        )

    @staticmethod
    def _as_list(data: Any) -> list:
        if not isinstance(data, list):
            raise InvalidResponse("The API answer was expected to be a list.")
        return data

    # -- writes (never retried) ------------------------------------------------------------------

    def import_eml(self, data: bytes) -> m.ImportResult:
        """409 is a normal outcome here (conflicting content is stored), not an error."""
        raw = self._request(
            "POST", "/messages/eml", content=data, content_type="message/rfc822", ok_statuses=(409,)
        )
        return self._parse(m.ImportResult, raw)

    def import_text(self, text: str) -> m.ImportResult:
        raw = self._request("POST", "/messages/text", json_body={"text": text}, ok_statuses=(409,))
        return self._parse(m.ImportResult, raw)

    def analyze(self, message_id: int) -> m.AnalyzeResponse:
        return self._parse(m.AnalyzeResponse, self._request("POST", f"/messages/{int(message_id)}/analyze"))

    def edit_case(
        self,
        case_id: int,
        expected_version: int,
        corrections: dict[str, list[str] | None],
        note: str | None = None,
    ) -> m.CaseDetail:
        body: dict[str, Any] = {"expected_version": expected_version, "corrections": corrections}
        if note:
            body["note"] = note
        return self._parse(m.CaseDetail, self._request("PATCH", f"/cases/{int(case_id)}", json_body=body))

    def approve(
        self,
        case_id: int,
        expected_version: int,
        note: str | None = None,
        override_reason: str | None = None,
    ) -> m.CaseDetail:
        body: dict[str, Any] = {"expected_version": expected_version}
        if note:
            body["note"] = note
        if override_reason:
            body["override_reason"] = override_reason
        return self._parse(
            m.CaseDetail, self._request("POST", f"/cases/{int(case_id)}/approve", json_body=body)
        )

    def reject(self, case_id: int, expected_version: int, reason: str) -> m.CaseDetail:
        return self._decide(case_id, "reject", expected_version, reason)

    def reopen(self, case_id: int, expected_version: int, reason: str) -> m.CaseDetail:
        return self._decide(case_id, "reopen", expected_version, reason)

    def _decide(self, case_id: int, action: str, expected_version: int, reason: str) -> m.CaseDetail:
        body = {"expected_version": expected_version, "reason": reason}
        return self._parse(m.CaseDetail, self._request("POST", f"/cases/{int(case_id)}/{action}", json_body=body))
