"""Excel export: selection rules, values, filters, failures and read-only behaviour.

Real FastAPI app, real service layer, temporary SQLite databases, fictional records only.
"""

from __future__ import annotations

import dataclasses
import re
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import pytest
from conftest import AMBIGUOUS_ORDER, CLEAN_COMPLAINT, INJECTION_TEXT, SAMPLES, StepClock, import_text, open_case
from export_helpers import (
    XLSX,
    ambiguous_order,
    approve,
    complaint,
    export_bytes,
    ids,
    new_approved,
    open_xlsx,
    snapshot_of_db,
    table,
)
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from ai_ops_hub.api import create_app
from ai_ops_hub.db.engine import create_db_engine, init_db
from ai_ops_hub.db.repositories import ExportRepository
from ai_ops_hub.export.workbook import DATA_SHEET, HEADERS, INFO_SHEET
from ai_ops_hub.services.export_service import MAX_EXPORT_CASES, ExportService


def error_of(r) -> dict:
    return r.json()["error"]


@pytest.fixture()
def cclient(engine, settings, clock):
    """Client with a deterministic clock: every operation happens one minute after the last."""
    with TestClient(create_app(settings=settings, engine=engine, clock=clock)) as c:
        yield c


# --- which cases are exported ------------------------------------------------------------------------


def test_only_approved_cases_are_exported(client):
    approved = new_approved(client, 1)
    rejected = open_case(client, complaint(2))
    client.post(f"/cases/{rejected['id']}/reject", json={"expected_version": 1, "reason": "spam message"})
    ready = open_case(client, complaint(3))
    needs = open_case(client, ambiguous_order(4))
    assert ready["status"] == "ready_for_review" and needs["status"] == "needs_review"
    r = client.get("/exports/approved-cases.xlsx")
    assert ids(r.content) == [approved]
    # the other states are simply not selected: they are not "skipped as inconsistent" either
    assert r.headers["x-export-skipped-count"] == "0"
    assert client.get("/exports/approved-cases/summary").json() | {} == {
        "eligible_count": 1, "skipped_count": 0, "max_export_cases": MAX_EXPORT_CASES,
        "exceeds_limit": False, "approved_from": None, "approved_to": None}


def test_an_approved_case_whose_decision_record_does_not_match_its_version_is_skipped(client, engine):
    good, bad = new_approved(client, 1), new_approved(client, 2)
    with engine.begin() as conn:  # the case moved on without a matching approval record
        conn.execute(text("UPDATE cases SET version = version + 1 WHERE id = :c"), {"c": bad})
    assert ids(export_bytes(client)) == [good]
    assert client.get("/exports/approved-cases/summary").json()["skipped_count"] == 1


def test_a_reopened_case_disappears_until_it_is_approved_again(client):
    cid = new_approved(client, 1)
    other = new_approved(client, 2)
    assert ids(export_bytes(client)) == [cid, other]

    case = client.get(f"/cases/{cid}").json()["case"]
    client.post(f"/cases/{cid}/reopen", json={"expected_version": case["version"], "reason": "wrong amount"})
    assert ids(export_bytes(client)) == [other]

    case = client.get(f"/cases/{cid}").json()["case"]
    approve(client, cid, case["version"])
    rows = {r["ID przypadku"]: r for r in table(export_bytes(client))}
    assert set(rows) == {cid, other}
    assert rows[cid]["Wersja przypadku"] == 4  # approved -> reopened -> approved again
    assert "wrong amount" in rows[cid]["Pozostałe notatki"]


def test_reopened_case_with_a_correction_exports_the_new_snapshot_not_the_old_one(client):
    cid = new_approved(client, 1)
    v = client.get(f"/cases/{cid}").json()["case"]["version"]
    client.post(f"/cases/{cid}/reopen", json={"expected_version": v, "reason": "reference was wrong"})
    v = client.get(f"/cases/{cid}").json()["case"]["version"]
    r = client.patch(f"/cases/{cid}", json={"expected_version": v, "corrections": {"reference": ["FV/2026/9999"]}})
    assert r.status_code == 200
    approve(client, cid, r.json()["case"]["version"])
    (row,) = table(export_bytes(client))
    assert row["Numer referencyjny"] == "FV/2026/9999"
    assert "FV/2026/0001" in row["Poprawki człowieka"]  # what the analyzer said stays visible


def test_conflicting_imports_are_not_exported(client):
    ok = client.post("/messages/eml", content=(SAMPLES / "01_invoice_dispute_pl.eml").read_bytes(),
                     headers={"Content-Type": "message/rfc822"})
    assert ok.status_code in (200, 201)
    conflict = client.post("/messages/eml", content=(SAMPLES / "02_invoice_dispute_pl_conflict.eml").read_bytes(),
                           headers={"Content-Type": "message/rfc822"})
    assert conflict.status_code == 409
    mid = ok.json()["message"]["id"]
    case = client.post(f"/messages/{mid}/analyze").json()["case"]
    client.patch(f"/cases/{case['id']}", json={"expected_version": 1, "corrections": {"category": ["complaint"]}, "note": "ok"})
    assert client.get(f"/cases/{case['id']}").status_code == 200
    # the conflicting variant can never become a case, so it can never be exported
    cid = conflict.json()["message"]["id"]
    assert client.post(f"/messages/{cid}/analyze").status_code == 409
    assert client.get(f"/messages/{cid}").json()["message"]["import_status"] == "conflict"
    summary = client.get("/exports/approved-cases/summary").json()
    assert summary["eligible_count"] == 0


def test_the_selection_query_itself_excludes_messages_marked_as_conflicts(client, engine):
    """Defence in depth: even if a case somehow points at a conflict message, it is not selected."""
    keep, drop = new_approved(client, 1), new_approved(client, 2)
    with engine.begin() as conn:  # corrupt the data on purpose, behind the services' back
        conn.execute(text("UPDATE messages SET import_status='conflict', conflicts_with_id=1 WHERE id=2"))
    assert ids(export_bytes(client)) == [keep]
    assert client.get("/exports/approved-cases/summary").json()["eligible_count"] == 1
    assert drop not in ids(export_bytes(client))


# --- which values are exported --------------------------------------------------------------------------


def test_human_corrections_take_precedence_over_the_analysis(client):
    case = open_case(client, AMBIGUOUS_ORDER)  # deadline '15/12' is ambiguous, no product extracted? keep both visible
    edit = client.patch(
        f"/cases/{case['id']}",
        json={"expected_version": 1, "corrections": {"deadline": ["2026-12-15"], "product": ["Pompa Alfa Pro"],
                                                     "quantity": ["12 pcs"]}, "note": "Termin potwierdzony telefonicznie."},
    )
    assert edit.status_code == 200, edit.text
    approve(client, case["id"], edit.json()["case"]["version"], note="Zatwierdzam po rozmowie.")
    (row,) = table(export_bytes(client))
    assert row["Produkt / usługa"] == "Pompa Alfa Pro"
    assert row["Ilość"] == 12 and row["Jednostka"] == "pcs"
    assert row["Termin realizacji"] == datetime(2026, 12, 15)
    assert row["Zatwierdzono mimo problemów"] == "NIE" and row["Pola do weryfikacji (niepotwierdzone)"] is None
    assert "Termin realizacji: analiza „--12-15” → człowiek „2026-12-15”" in row["Poprawki człowieka"]
    assert "Produkt / usługa: analiza" in row["Poprawki człowieka"]
    assert row["Notatka przy zatwierdzeniu"] == "Zatwierdzam po rozmowie."
    assert "Termin potwierdzony telefonicznie." in row["Pozostałe notatki"]
    assert "--12-15" not in str(row["Termin realizacji"])


def test_a_corrected_category_is_the_exported_category(client):
    case = open_case(client, complaint(1))
    r = client.patch(f"/cases/{case['id']}", json={"expected_version": 1, "corrections": {"category": ["inquiry"]}})
    approve(client, case["id"], r.json()["case"]["version"])
    (row,) = table(export_bytes(client))
    assert row["Kategoria"] == "Zapytanie" and "Kategoria: analiza „Reklamacja”" in row["Poprawki człowieka"]


def test_clean_case_values_and_formats(client):
    cid = new_approved(client, 42)
    (row,) = table(export_bytes(client))
    assert row["ID przypadku"] == cid and row["Kategoria"] == "Reklamacja"
    assert row["Nadawca"] == "anna@zielonadolina.example" and row["Temat e-maila"].startswith("Sprzeciw wobec faktury")
    assert row["Kwota"] == 12500 and row["Waluta"] == "PLN"
    assert row["Termin realizacji"] == datetime(2026, 11, 20)
    assert row["Numer referencyjny"] == "FV/2026/0042"
    assert isinstance(row["Data importu (UTC)"], datetime) and isinstance(row["Data zatwierdzenia (UTC)"], datetime)
    assert row["Data zatwierdzenia (UTC)"] >= row["Data importu (UTC)"]
    assert row["Zatwierdzono mimo problemów"] == "NIE"
    assert row["Produkt / usługa"] is None and row["Telefon"] is None  # not stated: left empty, not invented


def test_the_approval_date_is_the_decision_time_not_the_import_time(cclient, clock):
    case = open_case(cclient, complaint(1))
    approve(cclient, case["id"], 1)
    (row,) = table(export_bytes(cclient))
    with_imp = row["Data importu (UTC)"]
    assert row["Data zatwierdzenia (UTC)"] > with_imp
    assert row["Data importu (UTC)"] == datetime(2026, 11, 16, 10, 0)


def test_override_information_and_unconfirmed_values_are_preserved(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    approve(client, case["id"], 1, override_reason="Klient potwierdził termin w rozmowie.", note="Zatwierdzone z wyjątkiem.")
    (row,) = table(export_bytes(client))
    assert row["Zatwierdzono mimo problemów"] == "TAK"
    assert row["Uzasadnienie (override)"] == "Klient potwierdził termin w rozmowie."
    assert "field_needs_review" in row["Zastąpione problemy"] and "deadline" in row["Zastąpione problemy"]
    assert row["Termin realizacji"] == "[niepotwierdzone] --12-15"  # NOT a clean date
    assert "Termin realizacji: --12-15 (niejednoznaczne" in row["Pola do weryfikacji (niepotwierdzone)"]
    assert row["Produkt / usługa"] is not None  # confirmed analyzer value, exported as is


def test_an_ambiguous_value_confirmed_by_a_human_becomes_a_clean_value(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    r = client.patch(f"/cases/{case['id']}", json={"expected_version": 1, "corrections": {"deadline": ["2026-12-15"]}})
    approve(client, case["id"], r.json()["case"]["version"])
    (row,) = table(export_bytes(client))
    assert row["Termin realizacji"] == datetime(2026, 12, 15)
    assert row["Pola do weryfikacji (niepotwierdzone)"] is None


def test_a_prompt_injection_case_needs_and_shows_its_override(client):
    case = open_case(client, INJECTION_TEXT)
    approve(client, case["id"], 1, override_reason="Treść sprawdzona ręcznie, to zwykła prośba.")
    (row,) = table(export_bytes(client))
    assert row["Zatwierdzono mimo problemów"] == "TAK" and "possible_prompt_injection" in row["Zastąpione problemy"]


def test_notes_added_before_approval_are_kept(client):
    case = open_case(client, complaint(1))
    r = client.patch(f"/cases/{case['id']}", json={"expected_version": 1, "note": "Zadzwonić do klienta."})
    approve(client, case["id"], r.json()["case"]["version"], note="Gotowe.")
    (row,) = table(export_bytes(client))
    assert row["Notatka przy zatwierdzeniu"] == "Gotowe." and "Zadzwonić do klienta." in row["Pozostałe notatki"]


def test_rows_are_ordered_by_case_id_and_the_export_is_repeatable(client):
    for n in (3, 1, 2):
        new_approved(client, n)
    first, second = table(export_bytes(client)), table(export_bytes(client))
    assert [r["ID przypadku"] for r in first] == [1, 2, 3]
    assert [r["Numer referencyjny"] for r in first] == ["FV/2026/0003", "FV/2026/0001", "FV/2026/0002"]
    assert first == second


def test_information_sheet_counts_match_the_data(client):
    new_approved(client, 1)
    new_approved(client, 2)
    ws = open_xlsx(export_bytes(client))[INFO_SHEET]
    text_ = "\n".join(f"{a.value}: {b.value}" for a, b in ws.iter_rows(min_col=1, max_col=2))
    assert "Liczba przypadków w pliku: 2" in text_ and "rule_based" in text_


# --- hostile content ----------------------------------------------------------------------------------


def test_hostile_email_text_and_corrections_cannot_become_formulas(client):
    mail = (
        "From: anna@zielonadolina.example\n"
        "Subject: =cmd|' /C calc'!A0\nMessage-ID: <evil-1@zielonadolina.example>\n\n"
        "Please send us the invoice FV/2026/0777.\n"
    )
    case = open_case(client, mail)
    r = client.patch(
        f"/cases/{case['id']}",
        json={"expected_version": 1, "corrections": {"category": ["document_request"], "product": ["=1+1"],
                                                     "contact_name": ["@SUM(A1:A9)"], "organization": ["-2+3+cmd"]},
              "note": "+cmd|' /C calc'!A0"},
    )
    assert r.status_code == 200, r.text
    approve(client, case["id"], r.json()["case"]["version"], note="=1+1")
    content = export_bytes(client)
    wb = open_xlsx(content)
    for ws in wb.worksheets:
        assert all(c.data_type != "f" for row in ws.iter_rows() for c in row)
    (row,) = table(content)
    for header in ("Temat e-maila", "Produkt / usługa", "Osoba kontaktowa", "Organizacja",
                   "Notatka przy zatwierdzeniu"):
        assert str(row[header]).startswith("'"), header
    assert row["Pozostałe notatki"].endswith("+cmd|' /C calc'!A0")  # labelled, so it can not start a formula
    with zipfile.ZipFile(BytesIO(content)) as z:
        assert all("<f>" not in z.read(n).decode("utf-8") for n in z.namelist() if n.endswith(".xml"))


# --- the endpoint ------------------------------------------------------------------------------------


def test_download_headers_content_type_and_body(client):
    new_approved(client, 1)
    r = client.get("/exports/approved-cases.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"] == XLSX
    assert re.fullmatch(r'attachment; filename="approved-cases_\d{8}-\d{6}\.xlsx"', r.headers["content-disposition"])
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-export-case-count"] == "1" and r.headers["x-export-skipped-count"] == "0"
    assert int(r.headers["content-length"]) == len(r.content) and r.content[:2] == b"PK"
    assert open_xlsx(r.content).sheetnames == [DATA_SHEET, INFO_SHEET]


def test_filename_uses_the_service_clock(cclient):
    new_approved(cclient, 1)
    r = cclient.get("/exports/approved-cases.xlsx")
    stamp = re.search(r"approved-cases_(\d{8}-\d{6})\.xlsx", r.headers["content-disposition"]).group(1)
    assert stamp.startswith("20261116-10")  # StepClock starts 2026-11-16 10:00 UTC


def test_export_is_get_only(client):
    for method in ("post", "put", "patch", "delete"):
        r = getattr(client, method)("/exports/approved-cases.xlsx")
        assert r.status_code == 405 and error_of(r)["code"] == "method_not_allowed"


def test_no_approved_cases_gives_a_clear_409(client):
    open_case(client, complaint(1))  # open, not approved
    r = client.get("/exports/approved-cases.xlsx")
    assert r.status_code == 409 and error_of(r)["code"] == "no_approved_cases"
    assert "no approved cases" in error_of(r)["message"]
    assert client.get("/exports/approved-cases/summary").json()["eligible_count"] == 0


def test_summary_counts_and_limit(client):
    s = client.get("/exports/approved-cases/summary").json()
    assert s == {"eligible_count": 0, "skipped_count": 0, "max_export_cases": MAX_EXPORT_CASES,
                 "exceeds_limit": False, "approved_from": None, "approved_to": None}
    new_approved(client, 1)
    new_approved(client, 2)
    open_case(client, complaint(3))
    assert client.get("/exports/approved-cases/summary").json()["eligible_count"] == 2


# --- filters -------------------------------------------------------------------------------------------


def test_date_filters_select_by_approval_day_inclusive(cclient):
    # StepClock: each write is one minute later, all on 2026-11-16 (UTC) for a handful of cases
    new_approved(cclient, 1)
    new_approved(cclient, 2)
    day = "2026-11-16"
    assert ids(export_bytes(cclient, approved_from=day, approved_to=day)) == [1, 2]
    assert ids(export_bytes(cclient, approved_from=day)) == [1, 2]
    assert ids(export_bytes(cclient, approved_to=day)) == [1, 2]
    for params in ({"approved_from": "2026-11-17"}, {"approved_to": "2026-11-15"},
                   {"approved_from": "2026-12-01", "approved_to": "2026-12-31"}):
        r = cclient.get("/exports/approved-cases.xlsx", params=params)
        assert r.status_code == 409 and error_of(r)["code"] == "no_approved_cases", params
        assert "for this date range" in error_of(r)["message"]


def test_the_day_boundaries_are_utc_midnights(engine, settings):
    clock_times = iter([
        datetime(2026, 11, 15, 23, 59, tzinfo=timezone.utc), datetime(2026, 11, 15, 23, 59, tzinfo=timezone.utc),
        datetime(2026, 11, 15, 23, 59, tzinfo=timezone.utc),  # import / analyze / (approve below)
    ])
    current = {"now": datetime(2026, 11, 15, 23, 59, tzinfo=timezone.utc)}
    del clock_times
    with TestClient(create_app(settings=settings, engine=engine, clock=lambda: current["now"])) as c:
        a = open_case(c, complaint(1))
        approve(c, a["id"], 1)  # approved 2026-11-15 23:59
        current["now"] = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)
        b = open_case(c, complaint(2))
        approve(c, b["id"], 1)  # approved 2026-11-16 00:00
        assert ids(export_bytes(c, approved_to="2026-11-15")) == [a["id"]]
        assert ids(export_bytes(c, approved_from="2026-11-16")) == [b["id"]]


@pytest.mark.parametrize("params", [
    {"approved_from": "not-a-date"}, {"approved_to": "2026-13-45"}, {"approved_from": "16.11.2026"},
    {"approved_from": ""}, {"approved_to": "2026-11-16T10:00:00x"},
])
def test_malformed_filters_are_422_validation_errors(client, params):
    new_approved(client, 1)
    for path in ("/exports/approved-cases.xlsx", "/exports/approved-cases/summary"):
        r = client.get(path, params=params)
        assert r.status_code == 422 and error_of(r)["code"] == "validation_error", (path, params)


def test_a_range_that_ends_before_it_starts_is_refused(client):
    for path in ("/exports/approved-cases.xlsx", "/exports/approved-cases/summary"):
        r = client.get(path, params={"approved_from": "2026-12-01", "approved_to": "2026-11-01"})
        assert r.status_code == 422 and error_of(r)["code"] == "invalid_export_filter"


def test_unknown_query_parameters_are_ignored_not_trusted(client):
    new_approved(client, 1)
    new_approved(client, 2)
    assert ids(export_bytes(client, status="rejected", case_id="1", limit="1")) == [1, 2]


# --- size limit, large sets ------------------------------------------------------------------------------


def with_limit(app, engine, clock, limit: int):
    services = app.state.services
    app.state.services = dataclasses.replace(services, exports=ExportService(engine, clock, max_cases=limit))


def test_an_export_over_the_limit_is_refused_not_silently_truncated(engine, settings, clock):
    app = create_app(settings=settings, engine=engine, clock=clock)
    with_limit(app, engine, clock, 2)
    with TestClient(app) as c:
        for n in (1, 2):
            new_approved(c, n)
        assert ids(export_bytes(c)) == [1, 2]  # exactly at the limit is fine
        new_approved(c, 3)
        r = c.get("/exports/approved-cases.xlsx")
        assert r.status_code == 422 and error_of(r)["code"] == "export_too_large"
        assert error_of(r)["details"] == {"eligible": 3, "limit": 2}
        s = c.get("/exports/approved-cases/summary").json()
        assert s["exceeds_limit"] is True and s["eligible_count"] == 3 and s["max_export_cases"] == 2


def test_a_narrower_range_makes_an_oversized_export_possible(engine, settings):
    current = {"now": datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)}
    app = create_app(settings=settings, engine=engine, clock=lambda: current["now"])
    with_limit(app, engine, lambda: current["now"], 2)
    with TestClient(app) as c:
        for n, day in ((1, 14), (2, 15), (3, 16)):
            current["now"] = datetime(2026, 11, day, 10, 0, tzinfo=timezone.utc)
            new_approved(c, n)
        assert c.get("/exports/approved-cases.xlsx").status_code == 422
        assert ids(export_bytes(c, approved_from="2026-11-15")) == [2, 3]


def test_a_larger_dataset_exports_completely(client):
    for n in range(1, 61):
        new_approved(client, n)
    content = export_bytes(client)
    assert ids(content) == list(range(1, 61))
    assert open_xlsx(content)[DATA_SHEET].auto_filter.ref == f"A1:Z61"


# --- failures ---------------------------------------------------------------------------------------


def test_a_database_error_is_a_503_without_internals(client, monkeypatch):
    new_approved(client, 1)

    def boom(self, *a, **k):
        raise OperationalError("SELECT secret_table FROM /home/secret/path.db", {}, Exception("disk I/O error"))

    monkeypatch.setattr(ExportRepository, "count_approved", boom)
    for path in ("/exports/approved-cases.xlsx", "/exports/approved-cases/summary"):
        r = client.get(path)
        assert r.status_code == 503 and error_of(r)["code"] == "export_database_error"
        assert "secret" not in r.text and "disk I/O" not in r.text and "SELECT" not in r.text


def test_a_database_error_in_a_later_query_is_also_a_503(client, monkeypatch):
    new_approved(client, 1)

    def boom(self, *a, **k):
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(ExportRepository, "reviews_by_case", boom)
    r = client.get("/exports/approved-cases.xlsx")
    assert r.status_code == 503 and error_of(r)["code"] == "export_database_error"


def test_a_generation_failure_is_a_500_without_internals(client, monkeypatch):
    new_approved(client, 1)
    import ai_ops_hub.services.export_service as svc

    def boom(*a, **k):
        raise RuntimeError(r"secret failure at C:\private\path")

    monkeypatch.setattr(svc, "build_workbook", boom)
    r = client.get("/exports/approved-cases.xlsx")
    assert r.status_code == 500 and error_of(r)["code"] == "export_generation_failed"
    assert "secret" not in r.text and "private" not in r.text
    assert r.headers["content-type"].startswith("application/json")  # never a half-written file


def test_a_failed_export_changes_nothing_and_a_retry_works(client, engine, monkeypatch):
    new_approved(client, 1)
    before = snapshot_of_db(engine)
    import ai_ops_hub.services.export_service as svc

    real = svc.build_workbook
    monkeypatch.setattr(svc, "build_workbook", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert client.get("/exports/approved-cases.xlsx").status_code == 500
    monkeypatch.setattr(svc, "build_workbook", real)
    assert ids(export_bytes(client)) == [1]
    assert snapshot_of_db(engine) == before


def test_an_inconsistent_approval_is_skipped_counted_and_never_exported_on_trust(client, engine):
    good = new_approved(client, 1)
    bad = new_approved(client, 2)
    # Someone writes a correction behind the service's back AFTER the approval: the approval
    # snapshot no longer matches the effective data, so this case must not be exported.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO case_corrections (case_id, target, action, \"values\", previous_values, version_after, reviewer, created_at)"
            " VALUES (:c, 'product', 'set', '[\"Tampered\"]', '[]', 99, 'x', '2026-11-16 12:00:00')"), {"c": bad})
    assert ids(export_bytes(client)) == [good]
    s = client.get("/exports/approved-cases/summary").json()
    assert s["eligible_count"] == 1 and s["skipped_count"] == 1
    ws = open_xlsx(export_bytes(client))[INFO_SHEET]
    text_ = "\n".join(f"{a.value}: {b.value}" for a, b in ws.iter_rows(min_col=1, max_col=2))
    assert "Pominięte (niespójny zapis zatwierdzenia): 1" in text_


def test_when_every_approved_case_is_inconsistent_the_export_says_so(client, engine):
    bad = new_approved(client, 1)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO case_corrections (case_id, target, action, \"values\", previous_values, version_after, reviewer, created_at)"
            " VALUES (:c, 'product', 'set', '[\"Tampered\"]', '[]', 99, 'x', '2026-11-16 12:00:00')"), {"c": bad})
    r = client.get("/exports/approved-cases.xlsx")
    assert r.status_code == 409 and "skipped as inconsistent" in error_of(r)["message"]


# --- read-only behaviour and consistency ----------------------------------------------------------------


def test_exporting_changes_nothing_in_the_database(client, engine):
    new_approved(client, 1)
    rejected = open_case(client, complaint(2))
    client.post(f"/cases/{rejected['id']}/reject", json={"expected_version": 1, "reason": "duplicate request"})
    open_case(client, AMBIGUOUS_ORDER)
    before = snapshot_of_db(engine)
    stats = client.get("/cases/stats").json()
    for _ in range(3):
        export_bytes(client)
        client.get("/exports/approved-cases/summary")
        client.get("/exports/approved-cases.xlsx", params={"approved_from": "2030-01-01"})  # empty: 409
    assert snapshot_of_db(engine) == before
    assert client.get("/cases/stats").json() == stats
    assert client.get("/cases/1").json()["case"]["status"] == "approved"


def test_no_export_audit_event_claims_a_download(client, engine):
    new_approved(client, 1)
    export_bytes(client)
    with engine.connect() as conn:
        types = {r[0] for r in conn.execute(text("SELECT DISTINCT event_type FROM audit_events"))}
    assert not any("export" in t or "download" in t for t in types)


def test_cases_with_stale_versions_are_unaffected_by_an_export(client):
    cid = new_approved(client, 1)
    export_bytes(client)
    version = client.get(f"/cases/{cid}").json()["case"]["version"]
    assert version == 2  # created v1, approved v2; the export did not bump it
    r = client.post(f"/cases/{cid}/reopen", json={"expected_version": 2, "reason": "reopen after export"})
    assert r.status_code == 200


def test_the_exported_set_is_one_consistent_snapshot_even_if_cases_change_mid_export(file_engine, settings, clock, monkeypatch):
    """A case approved by someone else WHILE the export reads must not appear half-way."""
    app = create_app(settings=settings, engine=file_engine, clock=clock)
    with TestClient(app) as c:
        a, b = new_approved(c, 1), new_approved(c, 2)
        late = open_case(c, complaint(3))
        original = ExportRepository.corrections_by_case
        fired = {"n": 0}

        def approve_in_the_middle(self, case_ids):
            if not fired["n"]:
                fired["n"] = 1
                # a second connection commits while this export's read transaction is open
                r = c.post(f"/cases/{late['id']}/approve", json={"expected_version": 1})
                assert r.status_code == 200
            return original(self, case_ids)

        monkeypatch.setattr(ExportRepository, "corrections_by_case", approve_in_the_middle)
        content = export_bytes(c)
        assert ids(content) == [a, b]  # the late approval is not in this file ...
        monkeypatch.setattr(ExportRepository, "corrections_by_case", original)
        assert ids(export_bytes(c)) == [a, b, late["id"]]  # ... but it is in the next one


def test_the_header_row_of_a_real_export_is_the_documented_one(client):
    new_approved(client, 1)
    ws = open_xlsx(export_bytes(client))[DATA_SHEET]
    assert tuple(c.value for c in ws[1]) == HEADERS
