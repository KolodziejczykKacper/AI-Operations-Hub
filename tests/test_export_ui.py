"""Excel export in the UI: the HTTP client (mocked transport) and the Streamlit screen (AppTest
against the real FastAPI app in memory). Fictional data only; no network, no API keys."""

from __future__ import annotations

import dataclasses
import re
from datetime import date, datetime, timezone
from pathlib import Path

import httpx2 as httpx
import pytest
from conftest import AMBIGUOUS_ORDER, open_case
from export_helpers import XLSX, approve, complaint, ids, new_approved, open_xlsx, snapshot_of_db
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

import ai_ops_hub.services.export_service as export_service
from ai_ops_hub.api import create_app
from ai_ops_hub.services.export_service import ExportService
from ai_ops_hub.ui import client as C
from ai_ops_hub.ui.client import ApiClient
from ai_ops_hub.ui.views.export_page import cases_pl

APP = Path(__file__).resolve().parents[1] / "src" / "ai_ops_hub" / "ui" / "streamlit_app.py"
PAGE = "Eksport do Excela"
GENERATE, DOWNLOAD = "export_generate", "export_download"
ZIP_BYTES = b"PK\x03\x04" + b"0" * 50


# --- the client, with a mocked transport ----------------------------------------------------------------


def make(handler) -> tuple[ApiClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(wrapped))
    return ApiClient("http://127.0.0.1:8000", http=http), seen


def xlsx_response(body: bytes = ZIP_BYTES, **headers) -> httpx.Response:
    base = {
        "content-type": XLSX,
        "content-disposition": 'attachment; filename="approved-cases_20261117-100000.xlsx"',
        "x-export-case-count": "3",
        "x-export-skipped-count": "1",
    }
    return httpx.Response(200, content=body, headers=base | headers)


def err(status: int, code: str, message: str = "boom", details=None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message, "details": details}})


def test_client_downloads_the_file_and_reads_its_metadata():
    c, seen = make(lambda r: xlsx_response())
    f = c.export_approved_cases()
    assert f.content == ZIP_BYTES and f.filename == "approved-cases_20261117-100000.xlsx"
    assert f.case_count == 3 and f.skipped_count == 1
    assert seen[0].method == "GET" and seen[0].url.path == "/exports/approved-cases.xlsx"
    assert seen[0].headers["accept"] == XLSX


def test_client_sends_the_date_filters_as_iso_dates_and_omits_empty_ones():
    c, seen = make(lambda r: xlsx_response())
    c.export_approved_cases()
    assert seen[-1].url.query == b""
    c.export_approved_cases(approved_from=date(2026, 11, 1), approved_to=date(2026, 11, 30))
    assert dict(seen[-1].url.params) == {"approved_from": "2026-11-01", "approved_to": "2026-11-30"}
    c.export_approved_cases(approved_to=date(2026, 11, 30))
    assert dict(seen[-1].url.params) == {"approved_to": "2026-11-30"}


@pytest.mark.parametrize("headers, body", [
    ({"content-type": "application/json"}, ZIP_BYTES),
    ({"content-type": "text/html"}, b"<html>PK</html>"),
    ({}, b"not a zip file at all"),
    ({}, b""),
])
def test_client_refuses_an_answer_that_is_not_an_excel_file(headers, body):
    c, _ = make(lambda r: xlsx_response(body, **headers))
    with pytest.raises(C.InvalidResponse):
        c.export_approved_cases()


@pytest.mark.parametrize("status, code", [(409, "no_approved_cases"), (422, "export_too_large"),
                                          (422, "invalid_export_filter"), (503, "export_database_error"),
                                          (500, "export_generation_failed")])
def test_client_maps_error_envelopes_to_typed_errors(status, code):
    c, _ = make(lambda r: err(status, code, "m", {"limit": 2}))
    with pytest.raises(C.ApiError) as info:
        c.export_approved_cases()
    assert info.value.status == status and info.value.code == code and info.value.details == {"limit": 2}


def test_client_survives_a_non_json_error_body():
    c, _ = make(lambda r: httpx.Response(502, content=b"<html>bad gateway</html>", headers={"content-type": "text/html"}))
    with pytest.raises(C.ApiError) as info:
        c.export_approved_cases()
    assert info.value.status == 502 and info.value.code == "http_error"


def test_client_does_not_treat_a_redirect_as_a_file():
    c, _ = make(lambda r: httpx.Response(302, headers={"location": "http://evil.example/x.xlsx"}))
    with pytest.raises(C.ApiError):
        c.export_approved_cases()


@pytest.mark.parametrize("exc, text_", [(httpx.ConnectError("refused"), "Cannot connect"),
                                         (httpx.ReadTimeout("slow"), "did not answer in time")])
def test_client_reports_an_unreachable_backend_and_does_not_retry(exc, text_):
    calls = []

    def handler(request):
        calls.append(request)
        raise exc

    c, _ = make(handler)
    with pytest.raises(C.BackendUnavailable) as info:
        c.export_approved_cases()
    assert text_ in str(info.value) and len(calls) == 1
    assert info.value.outcome_unknown is False  # a GET changes nothing, so nothing is "unknown"


@pytest.mark.parametrize("disposition, expected", [
    ('attachment; filename="approved-cases_20261117-100000.xlsx"', "approved-cases_20261117-100000.xlsx"),
    ('attachment; filename="../../evil.xlsx"', C.FALLBACK_FILENAME),
    ('attachment; filename="evil.exe"', C.FALLBACK_FILENAME),
    ('attachment; filename="a b.xlsx"', C.FALLBACK_FILENAME),
    ('attachment; filename="..xlsx"', C.FALLBACK_FILENAME),
    ('attachment; filename="C:\\Windows\\x.xlsx"', C.FALLBACK_FILENAME),
    ("attachment", C.FALLBACK_FILENAME),
    ("", C.FALLBACK_FILENAME),
])
def test_client_only_accepts_plain_safe_file_names(disposition, expected):
    c, _ = make(lambda r: xlsx_response(**{"content-disposition": disposition}))
    assert c.export_approved_cases().filename == expected


def test_client_tolerates_garbage_count_headers():
    c, _ = make(lambda r: xlsx_response(**{"x-export-case-count": "many", "x-export-skipped-count": "-4"}))
    f = c.export_approved_cases()
    assert f.case_count == 0 and f.skipped_count == 0


def test_client_summary_parses_and_rejects_malformed_answers():
    body = {"eligible_count": 2, "skipped_count": 0, "max_export_cases": 500, "exceeds_limit": False, "extra": 1}
    c, seen = make(lambda r: httpx.Response(200, json=body))
    s = c.export_summary(approved_from=date(2026, 11, 1))
    assert s.eligible_count == 2 and s.max_export_cases == 500
    assert seen[0].url.path == "/exports/approved-cases/summary" and dict(seen[0].url.params) == {"approved_from": "2026-11-01"}
    bad, _ = make(lambda r: httpx.Response(200, json={"eligible_count": "many"}))
    with pytest.raises(C.InvalidResponse):
        bad.export_summary()


def test_the_client_never_posts_anything_for_an_export():
    c, seen = make(lambda r: xlsx_response() if r.url.path.endswith(".xlsx") else httpx.Response(
        200, json={"eligible_count": 1, "max_export_cases": 5, "exceeds_limit": False}))
    c.export_summary()
    c.export_approved_cases()
    assert {r.method for r in seen} == {"GET"}


# --- the screen (AppTest) ---------------------------------------------------------------------------------


class Rec(ApiClient):
    """Logs every HTTP round trip (method, path, query params)."""

    def __init__(self, http) -> None:
        super().__init__("http://127.0.0.1:8000", http=http)
        self.calls: list[tuple[str, str, dict | None]] = []

    def _send(self, method, path, **kw):
        self.calls.append((method, path, kw.get("params")))
        return super()._send(method, path, **kw)

    def n(self, path_end: str) -> int:
        return sum(1 for _m, p, _q in self.calls if p.endswith(path_end))


@pytest.fixture()
def rec(client) -> Rec:
    return Rec(client)


def start(rec: ApiClient, page: str = PAGE) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.session_state["client"] = rec
    at.session_state["page"] = page
    return at.run()


def goto(at: AppTest, page: str) -> AppTest:
    at.session_state["page"] = page
    return at.run()


def downloads(at: AppTest):
    return at.get("download_button")


def generate(at: AppTest) -> AppTest:
    return at.button(key=GENERATE).click().run()


def texts(at: AppTest) -> str:
    out = []
    for kind in ("markdown", "caption", "success", "info", "warning", "error", "header"):
        out += [str(e.value) for e in getattr(at, kind)]
    return "\n".join(out).replace("\\", "")


def metric(at: AppTest) -> str:
    return at.metric[0].value


def workbook_of(at: AppTest):
    return at.session_state["x_result"]["file"].content


def test_the_screen_is_in_the_navigation_and_renders_cleanly(rec):
    at = start(rec)
    assert not at.exception and not at.error
    assert PAGE in at.sidebar.radio[0].options
    assert at.header[0].value == "Eksport do Excela"
    assert "tylko zatwierdzone" in texts(at) and "niczego nie wysyła" in texts(at)


def test_with_nothing_approved_it_says_so_and_cannot_generate(rec, client):
    open_case(client, complaint(1))  # open, not approved
    at = start(rec)
    assert metric(at) == "0" and "Nie ma jeszcze zatwierdzonych przypadków" in texts(at)
    assert at.button(key=GENERATE).disabled
    assert rec.n(".xlsx") == 0 and not downloads(at)


def test_it_shows_the_exact_number_of_eligible_cases(rec, client):
    new_approved(client, 1)
    new_approved(client, 2)
    rejected = open_case(client, complaint(3))
    client.post(f"/cases/{rejected['id']}/reject", json={"expected_version": 1, "reason": "spam message"})
    open_case(client, AMBIGUOUS_ORDER)
    at = start(rec)
    assert metric(at) == "2" and not at.button(key=GENERATE).disabled


def test_loading_the_screen_never_generates_a_file(rec, client):
    new_approved(client, 1)
    at = start(rec)
    for _ in range(3):
        at.run()
    assert rec.n(".xlsx") == 0 and not downloads(at)


def test_clicking_generates_exactly_one_file_and_offers_the_download(rec, client):
    a, b = new_approved(client, 1), new_approved(client, 2)
    at = generate(start(rec))
    assert not at.exception and not at.error
    assert rec.n("approved-cases.xlsx") == 1
    assert "Plik jest gotowy: 2 przypadki" in texts(at)
    (dl,) = downloads(at)
    assert dl.proto.label == "Pobierz plik Excel (.xlsx)" and dl.proto.ignore_rerun is True
    assert dl.proto.url.endswith(".xlsx")
    assert ids(workbook_of(at)) == [a, b]
    assert at.session_state["x_result"]["file"].filename.startswith("approved-cases_")


def test_it_does_not_claim_the_file_was_downloaded(rec, client):
    new_approved(client, 1)
    shown = texts(generate(start(rec)))
    assert "gotowy" in shown and "wygenerowa" in shown
    assert "pobrano" not in shown.lower() and "pobrany" not in shown.lower()


def test_reruns_and_navigation_do_not_regenerate_or_refetch_the_file(rec, client):
    new_approved(client, 1)
    at = generate(start(rec))
    for _ in range(4):
        at.run()
    goto(at, "Panel")
    goto(at, "Przypadki")
    at = goto(at, PAGE)
    assert rec.n("approved-cases.xlsx") == 1  # one click, one generation
    assert len(downloads(at)) == 1  # the same file is still offered, from the session


def test_two_clicks_are_two_generations_never_more(rec, client):
    new_approved(client, 1)
    at = generate(start(rec))
    generate(at)
    assert rec.n("approved-cases.xlsx") == 2


def test_the_user_can_get_a_fresh_file_after_the_data_changed(rec, client):
    new_approved(client, 1)
    at = generate(start(rec))
    new_approved(client, 2)
    at.run()
    assert "zmieniła się liczba zatwierdzonych przypadków (teraz: 2)" in texts(at)
    at = generate(at)
    assert ids(workbook_of(at)) == [1, 2] and "zmieniła się liczba" not in texts(at)


def test_exporting_through_the_ui_changes_nothing(rec, client, engine):
    new_approved(client, 1)
    open_case(client, AMBIGUOUS_ORDER)
    before = snapshot_of_db(engine)
    at = generate(start(rec))
    at.run()
    assert snapshot_of_db(engine) == before
    assert all(method == "GET" for method, _p, _q in rec.calls)


def test_the_ui_uses_only_get_requests_to_the_export_endpoints(rec, client):
    new_approved(client, 1)
    generate(start(rec))
    paths = {p for _m, p, _q in rec.calls}
    assert "/exports/approved-cases.xlsx" in paths and "/exports/approved-cases/summary" in paths


# --- filters in the UI ---------------------------------------------------------------------------------


def test_date_range_filter_is_passed_to_the_api_and_limits_the_file(rec, client):
    new_approved(client, 1)
    today = datetime.now(timezone.utc).date()
    at = start(rec)
    at.checkbox(key="x_use_range").check().run()
    at.date_input(key="x_from").set_value(date(2020, 1, 1))
    at.date_input(key="x_to").set_value(date(2020, 1, 31)).run()
    assert metric(at) == "0" and at.button(key=GENERATE).disabled  # nothing approved in January 2020
    at.date_input(key="x_from").set_value(today)
    at.date_input(key="x_to").set_value(today).run()
    assert metric(at) == "1"
    generate(at)
    last = next(q for _m, p, q in reversed(rec.calls) if p.endswith("approved-cases.xlsx"))
    assert last == {"approved_from": today.isoformat(), "approved_to": today.isoformat()}
    assert ids(workbook_of(at)) == [1]


def test_a_backwards_date_range_shows_the_servers_message(rec, client):
    new_approved(client, 1)
    at = start(rec)
    at.checkbox(key="x_use_range").check().run()
    at.date_input(key="x_from").set_value(date(2026, 12, 1))
    at.date_input(key="x_to").set_value(date(2026, 11, 1)).run()
    assert not at.exception
    assert "Nieprawidłowy zakres dat" in texts(at) and "must not be later" in texts(at)
    assert rec.n(".xlsx") == 0


def test_changing_the_filter_hides_a_file_that_no_longer_matches(rec, client):
    new_approved(client, 1)
    at = generate(start(rec))
    assert len(downloads(at)) == 1
    at.checkbox(key="x_use_range").check().run()
    assert not downloads(at) and "Zmieniono zakres dat" in texts(at)


# --- failures -----------------------------------------------------------------------------------------


def test_no_approved_cases_at_click_time_is_a_clear_warning(rec, client):
    cid = new_approved(client, 1)
    stale_summary = rec.export_summary()  # the screen still believes there is 1 case ...
    rec.export_summary = lambda **kw: stale_summary
    at = start(rec)
    assert metric(at) == "1"
    v = client.get(f"/cases/{cid}").json()["case"]["version"]
    client.post(f"/cases/{cid}/reopen", json={"expected_version": v, "reason": "reopened meanwhile"})
    at = generate(at)  # ... so the server decides at click time
    assert not at.exception and not downloads(at)
    assert "Brak zatwierdzonych przypadków" in " ".join(str(e.value) for e in at.warning)


def test_a_generation_failure_is_reported_without_internals(rec, client, monkeypatch):
    new_approved(client, 1)
    at = start(rec)

    def boom(*a, **k):
        raise RuntimeError(r"secret failure at C:\private\path")

    monkeypatch.setattr(export_service, "build_workbook", boom)
    at = generate(at)
    assert not at.exception
    shown = " ".join(str(e.value) for e in at.error).replace("\\", "")
    assert "export_generation_failed" in shown and "secret" not in shown and "private" not in shown
    assert not downloads(at) and "x_result" not in at.session_state


def test_a_failed_generation_clears_an_older_file_instead_of_offering_it(rec, client, monkeypatch):
    new_approved(client, 1)
    at = generate(start(rec))
    assert len(downloads(at)) == 1
    monkeypatch.setattr(export_service, "build_workbook", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    at = generate(at)
    assert at.error and not downloads(at)


def test_an_unreachable_backend_during_generation_is_an_error_not_a_crash(rec, client):
    new_approved(client, 1)
    at = start(rec)
    real = rec._send

    def failing(method, path, **kw):
        if path.endswith(".xlsx"):
            raise C.BackendUnavailable("Cannot connect to the API at http://127.0.0.1:8000. Is the backend running?")
        return real(method, path, **kw)

    rec._send = failing
    at = generate(at)
    assert not at.exception and "Nie udało się połączyć z API" in " ".join(str(e.value) for e in at.error)
    assert not downloads(at)


def test_a_database_failure_on_the_screen_stops_it_with_a_clear_message(rec, client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from ai_ops_hub.db.repositories import ExportRepository

    new_approved(client, 1)
    monkeypatch.setattr(ExportRepository, "count_approved",
                        lambda self, *a, **k: (_ for _ in ()).throw(OperationalError("SELECT secret", {}, Exception("io"))))
    at = start(rec)
    assert not at.exception
    shown = " ".join(str(e.value) for e in at.error).replace("\\", "")
    assert "export_database_error" in shown and "secret" not in shown


def test_too_many_cases_blocks_generation_and_suggests_narrowing(engine, settings, clock):
    app = create_app(settings=settings, engine=engine, clock=clock)
    app.state.services = dataclasses.replace(app.state.services, exports=ExportService(engine, clock, max_cases=1))
    with TestClient(app) as c:
        new_approved(c, 1)
        new_approved(c, 2)
        rec = Rec(c)
        at = start(rec)
        assert at.button(key=GENERATE).disabled
        assert "Za dużo przypadków" in texts(at) and "limit: 1" in texts(at)
        assert rec.n(".xlsx") == 0


def test_inconsistent_cases_are_reported_on_the_screen(rec, client, engine):
    from sqlalchemy import text

    good, bad = new_approved(client, 1), new_approved(client, 2)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO case_corrections (case_id, target, action, \"values\", previous_values, version_after, reviewer, created_at)"
            " VALUES (:c, 'product', 'set', '[\"Tampered\"]', '[]', 99, 'x', '2026-11-16 12:00:00')"), {"c": bad})
    at = start(rec)
    assert metric(at) == "1" and "Pominięte zatwierdzone przypadki: 1" in texts(at)
    at = generate(at)
    assert ids(workbook_of(at)) == [good]


# --- content and safety ---------------------------------------------------------------------------------


def test_a_hostile_case_exported_through_the_ui_has_no_formula_cells(rec, client):
    mail = (
        "From: anna@zielonadolina.example\nSubject: =HYPERLINK(\"http://evil.example\",\"x\")\n"
        "Message-ID: <ui-evil@zielonadolina.example>\n\nPlease send us the invoice FV/2026/0777.\n"
    )
    case = open_case(client, mail)
    r = client.patch(f"/cases/{case['id']}", json={"expected_version": 1, "corrections": {"category": ["document_request"]}})
    approve(client, case["id"], r.json()["case"]["version"], note="@SUM(A1)")
    wb = open_xlsx(workbook_of(generate(start(rec))))
    assert all(c.data_type != "f" for ws in wb.worksheets for row in ws.iter_rows() for c in row)


def test_the_screen_never_renders_untrusted_text_as_markup(rec, client):
    new_approved(client, 1)
    at = generate(start(rec))
    assert not any("<script" in str(e.value) for kind in ("markdown", "caption", "error", "warning", "info") for e in getattr(at, kind))


def test_the_screen_does_not_present_the_rule_based_analysis_as_ai(rec, client):
    new_approved(client, 1)
    shown = texts(generate(start(rec))).lower()
    assert "sztuczn" not in shown and "claude" not in shown and "gpt" not in shown


def test_the_ui_package_does_not_touch_the_database_or_build_workbooks_itself():
    root = Path(__file__).resolve().parents[1] / "src" / "ai_ops_hub" / "ui"
    forbidden = re.compile(r"^\s*(?:from|import)\s+(openpyxl|sqlalchemy|sqlmodel|sqlite3|ai_ops_hub\.(?:db|services|export|cases|api))\b", re.M)
    for path in root.rglob("*.py"):
        assert not forbidden.search(path.read_text(encoding="utf-8")), path


@pytest.mark.parametrize("n, expected", [(0, "0 przypadków"), (1, "1 przypadek"), (2, "2 przypadki"), (4, "4 przypadki"),
                                         (5, "5 przypadków"), (12, "12 przypadków"), (22, "22 przypadki"),
                                         (112, "112 przypadków"), (101, "101 przypadków")])
def test_polish_plural_of_cases(n, expected):
    assert cases_pl(n) == expected
