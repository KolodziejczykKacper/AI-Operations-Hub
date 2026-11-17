"""Streamlit UI tests (AppTest) against the REAL FastAPI app running in memory.

The UI is wired to the API through ``ApiClient`` over HTTP (TestClient is just an in-process
HTTP transport), so these tests exercise the same path as the real application without a
network. A recording client logs every request so tests can prove WHICH calls were made.
"""

from __future__ import annotations

from pathlib import Path

import httpx2 as httpx
import pytest
from conftest import (
    AMBIGUOUS_ORDER,
    CLEAN_COMPLAINT,
    SAMPLES,
    import_text,
    open_case,
)
from streamlit.testing.v1 import AppTest

from ai_ops_hub.ui import client as C
from ai_ops_hub.ui.client import ApiClient

APP = Path(__file__).resolve().parents[1] / "src" / "ai_ops_hub" / "ui" / "streamlit_app.py"
EVIL = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "![pixel](http://evil.example/p.png)",
    "[click](javascript:alert(1))",
]
MARKDOWN_KINDS = ("markdown", "caption", "warning", "error", "info", "success", "header", "subheader", "title")


class Recorder(ApiClient):
    """ApiClient that remembers every request (method, path, json body)."""

    def __init__(self, http) -> None:
        super().__init__("http://127.0.0.1:8000", http=http)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.params: list[tuple[str, dict | None]] = []

    def _request(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json_body")))
        self.params.append((path, kw.get("params")))
        return super()._request(method, path, **kw)

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.calls if m != "GET"]

    def body_of(self, method: str, path: str) -> dict | None:
        return next((b for m, p, b in reversed(self.calls) if (m, p) == (method, path)), None)


@pytest.fixture()
def rec(client) -> Recorder:
    return Recorder(client)


def start(rec: ApiClient, page: str | None = None, case: int | None = None) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.session_state["client"] = rec
    if page:
        at.session_state["page"] = page
    if case is not None:
        at.session_state["selected_case"] = case
    return at.run()


def goto(at: AppTest, page: str, case: int | None = None) -> AppTest:
    at.session_state["page"] = page
    if case is not None:
        at.session_state["selected_case"] = case
    return at.run()


def v(element) -> str:
    """Shown text with markdown escapes removed (the UI escapes untrusted text with backslashes)."""
    return str(element.value).replace("\\", "")


def texts(at: AppTest) -> list[str]:
    return [e.value for e in at.text]


def md_values(at: AppTest) -> list[str]:
    out: list[str] = []
    for kind in MARKDOWN_KINDS:
        out += [str(e.value) for e in getattr(at, kind)]
    out += [m.label for m in at.metric] + [v(m) for m in at.metric]
    out += [b.label for b in at.button] + [e.label for e in at.expander]
    out += [t.label for t in at.text_area] + [c.label for c in at.checkbox]
    return out


ORIGINAL, ANALYSIS, HISTORY = "1 · Oryginalny mail", "2 · Analiza i poprawki", "3 · Historia i audyt"


def section(at: AppTest, name: str) -> AppTest:
    """Switch the detail screen to one of its three sections (only the chosen one is rendered)."""
    return at.radio(key="detail_section").set_value(name).run()


def no_exception(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def button(at: AppTest, key: str):
    return at.button(key=key)


def keys(at: AppTest, kind: str) -> list[str]:
    return [e.key for e in getattr(at, kind) if e.key]


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def case_with(rec, text):
    return open_case(rec._http, text)


# --- app shell and dashboard ---------------------------------------------------------------------


def test_every_screen_renders_without_errors(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec)
    for page in ("Panel", "Import", "Przypadki", "Wiadomości"):
        goto(at, page)
        no_exception(at)
        assert not at.error, page
    goto(at, "Szczegóły przypadku", case["id"])
    no_exception(at)
    assert not at.error


def test_sidebar_always_says_the_analysis_is_rule_based_and_not_ai(rec):
    at = start(rec)
    side = " ".join(v(e) for e in at.sidebar.caption)
    assert "rule_based" in side and "nie model AI" in side and "local_demo_user" in side


def test_the_ui_never_presents_the_analysis_as_claude_or_ai(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    everything = " ".join(md_values(at) + texts(at))
    assert "Claude" not in everything
    assert "rule_based" in everything
    assert "nie jest" in everything and "model AI" in everything  # explicit disclaimer
    assert at.metric[3].value == "rule_based"


def test_dashboard_shows_exact_counts_from_the_api(rec):
    clean = case_with(rec, CLEAN_COMPLAINT)
    messy = case_with(rec, AMBIGUOUS_ORDER)
    rec.approve(clean["id"], 1)
    rec.reject(messy["id"], 1, "spam")
    case_with(rec, "From: z@x.example\nSubject: z\nMessage-ID: <z@x.example>\n\nHello there.")
    at = start(rec)
    m = metrics(at)
    assert m["Wszystkie przypadki"] == "3"
    assert m["🟢 Zatwierdzony"] == "1" and m["🔴 Odrzucony"] == "1" and m["🟠 Do poprawy"] == "1"
    assert m["🔵 Gotowy do przeglądu"] == "0"


def test_dashboard_totals_are_not_the_length_of_the_visible_list_page(rec):
    for i in range(20):
        case_with(rec, f"From: a@x.example\nSubject: case {i}\nMessage-ID: <d{i}@x.example>\n\nPlease send us the invoice FV/2026/{i:04d}.")
    at = start(rec)
    assert metrics(at)["Wszystkie przypadki"] == "20"
    goto(at, "Przypadki")
    assert len(keys(at, "button")) and len([k for k in keys(at, "button") if k.startswith("open_case_")]) == 15


def test_dashboard_with_no_cases_guides_the_user(rec):
    at = start(rec)
    assert metrics(at)["Wszystkie przypadki"] == "0"
    assert any("Import" in v(i) for i in at.info)


def test_dashboard_buttons_open_a_pre_filtered_list(rec):
    case_with(rec, CLEAN_COMPLAINT)
    case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec)
    button(at, "dash_needs").click().run()
    assert at.session_state["page"] == "Przypadki" and at.session_state["f_status"] == "needs_review"
    assert len([k for k in keys(at, "button") if k.startswith("open_case_")]) == 1


# --- reruns never change state ----------------------------------------------------------------------


def test_reruns_alone_never_send_state_changing_requests(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    rec.calls.clear()
    at = start(rec)
    for page in ("Panel", "Import", "Przypadki", "Wiadomości", "Szczegóły przypadku"):
        goto(at, page, case["id"] if page.startswith("Szcz") else None)
        at.run()
        at.run()
    assert rec.writes == []


def test_a_selected_file_or_typed_text_is_not_imported_by_reruns(rec):
    at = start(rec, "Import")
    at.file_uploader(key="eml_upload_0").upload("a.eml", (SAMPLES / "01_invoice_dispute_pl.eml").read_bytes()).run()
    at.run()
    at.run()
    at.text_area(key="import_text_0").set_value(CLEAN_COMPLAINT).run()
    at.run()
    assert rec.writes == []
    assert rec._http.get("/messages").json()["total"] == 0


def test_a_pending_decision_sends_nothing_until_it_is_confirmed(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku", case["id"])
    rec.calls.clear()
    button(at, "btn_approve").click().run()
    at.run()
    at.run()
    assert rec.writes == []
    assert "btn_confirm_approve" in keys(at, "button")
    assert rec._http.get(f"/cases/{case['id']}").json()["case"]["status"] == "ready_for_review"


def test_a_double_click_style_rerun_after_confirmation_does_not_approve_twice(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku", case["id"])
    button(at, "btn_approve").click().run()
    button(at, "btn_confirm_approve").click().run()
    at.run()
    at.run()
    assert rec.writes == [("POST", f"/cases/{case['id']}/approve")]


# --- import -------------------------------------------------------------------------------------------


def test_import_text_with_analysis(rec):
    at = start(rec, "Import")
    at.text_area(key="import_text_0").set_value(CLEAN_COMPLAINT).run()
    button(at, "btn_import_text").click().run()
    no_exception(at)
    assert rec.writes == [("POST", "/messages/text"), ("POST", "/messages/1/analyze")]
    assert any("Zaimportowano nową wiadomość #1" in v(s) and "rule_based" in v(s) for s in at.success)
    assert at.text_area(key="import_text_1").value == ""  # the box is empty for the next message
    button(at, "fb_open_case").click().run()
    assert at.session_state["page"] == "Szczegóły przypadku" and at.session_state["selected_case"] == 1


def test_import_text_without_analysis_when_unchecked(rec):
    at = start(rec, "Import")
    at.text_area(key="import_text_0").set_value(CLEAN_COMPLAINT)
    at.checkbox(key="analyze_after_text").uncheck().run()
    button(at, "btn_import_text").click().run()
    assert rec.writes == [("POST", "/messages/text")]
    assert rec._http.get("/cases").json()["total"] == 0
    assert any("nie została jeszcze przeanalizowana" in v(c) for c in at.caption)


def test_duplicate_import_is_reported_as_duplicate(rec):
    import_text(rec._http, CLEAN_COMPLAINT)
    at = start(rec, "Import")
    at.text_area(key="import_text_0").set_value(CLEAN_COMPLAINT)
    at.checkbox(key="analyze_after_text").uncheck().run()
    button(at, "btn_import_text").click().run()
    assert any("DUPLIKAT" in v(i) and "#1" in v(i) for i in at.info)
    assert rec._http.get("/messages").json()["total"] == 1


def test_conflicting_import_is_reported_and_never_analyzed(rec):
    import_text(rec._http, CLEAN_COMPLAINT)
    changed = CLEAN_COMPLAINT.replace("12 500,00", "99 999,00")
    at = start(rec, "Import")
    at.text_area(key="import_text_0").set_value(changed).run()
    button(at, "btn_import_text").click().run()
    no_exception(at)
    warn = " ".join(v(w) for w in at.warning)
    assert "KONFLIKT" in warn and "nie można analizować" in warn
    assert ("POST", "/messages/2/analyze") not in rec.writes
    assert rec._http.get("/cases").json()["total"] == 0
    assert "fb_open_case" not in keys(at, "button")


def test_eml_upload_imports_only_on_click(rec):
    at = start(rec, "Import")
    at.file_uploader(key="eml_upload_0").upload("01.eml", (SAMPLES / "01_invoice_dispute_pl.eml").read_bytes()).run()
    assert rec.writes == []
    button(at, "btn_import_file").click().run()
    no_exception(at)
    assert rec.writes[0] == ("POST", "/messages/eml")
    assert any("Zaimportowano" in v(s) for s in at.success)
    assert "eml_upload_1" in keys(at, "file_uploader") or at.session_state["upload_nonce"] == 1


def test_eml_duplicate_and_conflict_files(rec):
    at = start(rec, "Import")
    for name, expect in (("01_invoice_dispute_pl.eml", "Zaimportowano"), ("01_invoice_dispute_pl.eml", "DUPLIKAT"),
                         ("02_invoice_dispute_pl_conflict.eml", "KONFLIKT")):
        key = f"eml_upload_{at.session_state['upload_nonce']}"
        at.file_uploader(key=key).upload(name, (SAMPLES / name).read_bytes()).run()
        button(at, "btn_import_file").click().run()
        shown = " ".join(v(e) for e in (*at.success, *at.info, *at.warning))
        assert expect in shown, (name, shown)


def test_malformed_file_shows_a_clear_error_and_keeps_the_input(rec):
    at = start(rec, "Import")
    at.file_uploader(key="eml_upload_0").upload("bad.eml", b"\x00\x01 garbage without headers").run()
    button(at, "btn_import_file").click().run()
    assert any("nie wygląda na poprawną wiadomość" in v(e) for e in at.error)
    assert at.session_state["upload_nonce"] == 0  # not cleared: the user can fix and retry
    assert rec._http.get("/messages").json()["total"] == 0


def test_failed_text_import_keeps_what_the_user_typed(rec):
    at = start(rec, "Import")
    at.text_area(key="import_text_0").set_value("no headers and a body\n").run()
    at.text_area(key="import_text_0").set_value("Subject: x\n\n" + "z" * 250_000).run()
    button(at, "btn_import_text").click().run()
    assert any("rozmiar" in v(e) for e in at.error)
    assert at.text_area(key="import_text_0").value.startswith("Subject: x")


def test_import_buttons_are_disabled_without_input(rec):
    at = start(rec, "Import")
    assert button(at, "btn_import_file").disabled and button(at, "btn_import_text").disabled


# --- case list ------------------------------------------------------------------------------------------


def make_cases(rec, n: int, prefix: str = "report") -> None:
    for i in range(n):
        case_with(rec, f"From: a@x.example\nSubject: {prefix} {i}\nMessage-ID: <{prefix}{i}@x.example>\n\nPlease send us the invoice FV/2026/{i:04d}.")


def rows(at: AppTest) -> list[str]:
    return [k for k in keys(at, "button") if k.startswith("open_case_")]


def test_case_list_shows_id_subject_category_status_date_and_action(rec):
    case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Przypadki")
    cells = texts(at)
    assert "#1" in cells and any("Sprzeciw wobec faktury" in c for c in cells)
    assert "Reklamacja" in cells and any("Gotowy do przeglądu" in c for c in cells)
    assert any("UTC" in c for c in cells) and rows(at) == ["open_case_1"]


def test_case_list_pagination_fetches_one_page_at_a_time(rec):
    make_cases(rec, 17)
    at = start(rec, "Przypadki")
    assert len(rows(at)) == 15
    assert any("Strona 1 z 2" in v(c) and "razem: 17" in v(c) for c in at.caption)
    button(at, "cases_next").click().run()
    assert len(rows(at)) == 2 and any("Strona 2 z 2" in v(c) for c in at.caption)
    button(at, "cases_prev").click().run()
    assert len(rows(at)) == 15


def test_list_requests_ask_for_a_page_not_everything(rec):
    make_cases(rec, 30)
    at = start(rec, "Przypadki")
    asked = [p for path, p in rec.params if path == "/cases"]
    assert asked and all(p["limit"] == 15 and p["offset"] == 0 for p in asked)  # one page, never "everything"
    assert len(rows(at)) == 15  # 30 cases exist
    button(at, "cases_next").click().run()
    assert [p for path, p in rec.params if path == "/cases"][-1]["offset"] == 15


def test_status_filter_and_page_reset(rec):
    case_with(rec, CLEAN_COMPLAINT)
    make_cases(rec, 16, "messy order")  # clean-ish; all ready_for_review
    case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Przypadki")
    button(at, "cases_next").click().run()
    assert at.session_state["f_page"] == 2
    at.selectbox(key="f_status").select("needs_review").run()
    assert at.session_state["f_page"] == 1 and len(rows(at)) == 1
    at.selectbox(key="f_status").select("approved").run()
    assert any("Brak przypadków dla wybranych filtrów" in v(i) for i in at.info)


def test_search_by_subject(rec):
    make_cases(rec, 3, "alpha")
    make_cases(rec, 2, "bravo")
    at = start(rec, "Przypadki")
    at.text_input(key="f_q").set_value("ALPHA").run()
    assert len(rows(at)) == 3
    at.text_input(key="f_q").set_value("nothing-matches").run()
    assert rows(at) == []


def test_page_size_selector(rec):
    make_cases(rec, 12)
    at = start(rec, "Przypadki")
    at.selectbox(key="f_size").select(10).run()
    assert len(rows(at)) == 10 and any("Strona 1 z 2" in v(c) for c in at.caption)


def test_open_button_leads_to_the_case(rec):
    case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Przypadki")
    button(at, "open_case_1").click().run()
    assert at.session_state["page"] == "Szczegóły przypadku"
    assert at.header[0].value == "Szczegóły przypadku" or any("Przypadek #1" in h.value for h in at.header)


def test_filters_survive_a_visit_to_another_screen(rec):
    case_with(rec, CLEAN_COMPLAINT)
    case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Przypadki")
    at.selectbox(key="f_status").select("needs_review").run()
    at.text_input(key="f_q").set_value("order").run()
    goto(at, "Panel")
    goto(at, "Przypadki")
    assert at.selectbox(key="f_status").value == "needs_review" and at.text_input(key="f_q").value == "order"


def test_messages_screen_can_analyze_a_pending_message_and_flags_conflicts(rec):
    import_text(rec._http, CLEAN_COMPLAINT)
    rec._http.post("/messages/text", json={"text": CLEAN_COMPLAINT.replace("12 500", "1 500")})  # conflict
    at = start(rec, "Wiadomości")
    assert "msg_analyze_1" in keys(at, "button") and "msg_analyze_2" not in keys(at, "button")
    assert any("KONFLIKT" in t for t in texts(at))
    button(at, "msg_analyze_1").click().run()
    assert rec.writes == [("POST", "/messages/1/analyze")]
    assert any("przeanalizowana" in v(s) for s in at.success)
    assert "msg_open_1" in keys(at, "button")


# --- case details --------------------------------------------------------------------------------------------


def test_case_detail_separates_original_analysis_and_corrections(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    no_exception(at)
    assert list(at.radio(key="detail_section").options) == [ORIGINAL, ANALYSIS, HISTORY]
    assert at.radio(key="detail_section").value == ANALYSIS  # the section people need first
    table = at.dataframe[0].value
    assert list(table.columns)[:3] == ["Pole", "Wartość z analizy (rule_based)", "Poprawka człowieka"]
    deadline = table[table["Pole"] == "Termin"].iloc[0]
    assert deadline["Wartość z analizy (rule_based)"] == "--12-15" and "niejednoznaczne" in deadline["Status"]
    assert "[[15/12]]" in deadline["Fragment źródłowy"] and deadline["Poprawka człowieka"] == "—"
    assert deadline["Ostrzeżenia"]  # the analyzer's reason, e.g. year not stated
    section(at, ORIGINAL)
    body = at.text_area(key=f"body_{case['id']}")
    assert "Pump Alpha" in body.value and body.disabled  # original, read-only, separate from the analysis
    assert not at.dataframe


def test_case_detail_header_shows_status_version_method_and_problems(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    m = metrics(at)
    assert m["Status"].endswith("Do poprawy") and m["Wersja"] == "1" and m["Metoda analizy"] == "rule_based"
    assert any("Do rozwiązania przed zatwierdzeniem" in v(w) for w in at.warning)


def test_audit_history_tab_lists_events(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    rec.edit_case(case["id"], 1, {}, "first note")
    at = start(rec, "Szczegóły przypadku", case["id"])
    section(at, HISTORY)
    audit = at.dataframe[-1].value
    events = list(audit["Zdarzenie"])
    assert events[:3] == ["Wiadomość zaimportowana", "Analiza wykonana (rule_based)", "Przypadek utworzony"]
    assert "Notatka dodana" in events
    assert "human (local_demo_user)" in list(audit["Aktor"])


def test_opening_a_missing_case_is_handled(rec):
    at = start(rec, "Szczegóły przypadku", 999)
    no_exception(at)
    assert any("Nie można otworzyć przypadku #999" in v(e) for e in at.error)
    assert "selected_case" not in at.session_state


def test_detail_screen_without_a_selection_offers_open_by_number(rec):
    case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku")
    at.number_input(key="open_by_id").set_value(1).run()
    button(at, "btn_open_by_id").click().run()
    assert at.session_state["selected_case"] == 1 and metrics(at)["Wersja"] == "1"


# --- corrections ------------------------------------------------------------------------------------------------


def test_correcting_a_field_sends_only_what_changed_with_the_viewed_version(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    at.text_area(key="edit_deadline").set_value("2026-12-15").run()
    button(at, "btn_save_edit").click().run()
    no_exception(at)
    assert rec.body_of("PATCH", f"/cases/{case['id']}") == {
        "expected_version": 1, "corrections": {"deadline": ["2026-12-15"]},
    }
    assert any("Zapisano poprawki" in v(s) for s in at.success)
    assert metrics(at)["Wersja"] == "2" and metrics(at)["Status"].endswith("Gotowy do przeglądu")
    table = at.dataframe[0].value
    row = table[table["Pole"] == "Termin"].iloc[0]
    assert (row["Wartość z analizy (rule_based)"], row["Poprawka człowieka"]) == ("--12-15", "2026-12-15")
    assert at.text_area(key="edit_deadline").value == "2026-12-15"  # the form shows the effective value


def test_a_human_correction_never_changes_the_original_email_shown(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    section(at, ORIGINAL)
    before = at.text_area(key=f"body_{case['id']}").value
    section(at, ANALYSIS)
    at.text_area(key="edit_deadline").set_value("2026-12-15").run()
    button(at, "btn_save_edit").click().run()
    section(at, ORIGINAL)
    assert at.text_area(key=f"body_{case['id']}").value == before


def test_category_and_note_corrections(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    at.selectbox(key="edit_category").select("inquiry")
    at.text_area(key="edit_note").set_value("Customer only asked for a quote.").run()
    button(at, "btn_save_edit").click().run()
    assert rec.body_of("PATCH", f"/cases/{case['id']}") == {
        "expected_version": 1, "corrections": {"category": ["inquiry"]}, "note": "Customer only asked for a quote.",
    }
    assert metrics(at)["Kategoria"] == "Zapytanie"
    assert at.text_area(key="edit_note").value == ""


def test_revert_to_the_analyzers_value(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    rec.edit_case(case["id"], 1, {"quantity": ["12 pcs"]})
    at = start(rec, "Szczegóły przypadku", case["id"])
    at.checkbox(key="edit_revert_quantity").check().run()
    button(at, "btn_save_edit").click().run()
    assert rec.body_of("PATCH", f"/cases/{case['id']}")["corrections"] == {"quantity": None}
    assert at.text_area(key="edit_quantity").value == "10 pcs"


def test_invalid_value_shows_the_server_error_and_keeps_the_text(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    at.text_area(key="edit_deadline").set_value("15 December").run()
    button(at, "btn_save_edit").click().run()
    no_exception(at)
    err = " ".join(v(e) for e in at.error)
    assert "Operacja nie została wykonana" in err and "deadline" in err and "YYYY-MM-DD" in err
    assert at.text_area(key="edit_deadline").value == "15 December"  # not lost
    assert metrics(at)["Wersja"] == "1"
    assert rec._http.get(f"/cases/{case['id']}/history").json()["corrections"] == []


def test_save_without_changes_makes_no_request(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    rec.calls.clear()
    button(at, "btn_save_edit").click().run()
    assert rec.writes == [] and any("Brak zmian" in v(i) for i in at.info)


def test_closed_cases_cannot_be_edited_in_the_ui(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    rec.approve(case["id"], 1)
    at = start(rec, "Szczegóły przypadku", case["id"])
    assert "btn_save_edit" not in keys(at, "button") and "edit_deadline" not in keys(at, "text_area")
    assert any("Przypadek jest zamknięty" in v(i) for i in at.info)


def test_unsaved_edits_survive_a_visit_to_another_screen(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    at.text_area(key="edit_product").set_value("Pump Alpha X").run()
    at.text_area(key="edit_note").set_value("draft note").run()
    goto(at, "Przypadki")
    goto(at, "Szczegóły przypadku", case["id"])
    assert at.text_area(key="edit_product").value == "Pump Alpha X"
    assert at.text_area(key="edit_note").value == "draft note"
    assert rec.writes == []


def test_switching_to_another_case_does_not_leak_a_draft(rec):
    first = case_with(rec, AMBIGUOUS_ORDER)
    second = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku", first["id"])
    at.text_area(key="edit_product").set_value("DRAFT").run()
    goto(at, "Szczegóły przypadku", second["id"])
    assert at.text_area(key="edit_product").value == ""


# --- optimistic locking -----------------------------------------------------------------------------------------------


def test_stale_save_is_rejected_refreshed_and_keeps_the_users_draft(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    cid = case["id"]
    at = start(rec, "Szczegóły przypadku", cid)
    at.text_area(key="edit_product").set_value("My Pump").run()  # the user is typing...
    # ...meanwhile somebody else changes the case (version 1 -> 2)
    rec._http.patch(f"/cases/{cid}", json={"expected_version": 1, "corrections": {"quantity": ["99 pcs"]}, "note": "colleague"})
    button(at, "btn_save_edit").click().run()
    no_exception(at)

    assert rec.body_of("PATCH", f"/cases/{cid}")["expected_version"] == 1  # the version the user SAW
    assert any("zmieniony" in v(w) and "Nic nie zostało nadpisane" in v(w) for w in at.warning)
    server = rec._http.get(f"/cases/{cid}").json()
    assert server["case"]["version"] == 2
    values = {f["name"]: [v["value"] for v in f["values"]] for f in server["fields"]}
    assert values["quantity"] == ["99 pcs"] and values["product"] == ["Pump Alpha"]  # nothing overwritten, nothing lost
    assert metrics(at)["Wersja"] == "2"  # the screen shows the newest state
    assert at.text_area(key="edit_product").value == "My Pump"  # the unsaved draft is kept
    assert at.text_area(key="edit_quantity").value == "99 pcs"  # untouched fields show the new server value

    button(at, "btn_save_edit").click().run()  # the user re-submits consciously, now on version 2
    assert rec.body_of("PATCH", f"/cases/{cid}")["expected_version"] == 2
    assert metrics(at)["Wersja"] == "3"
    assert rec._http.get(f"/cases/{cid}").json()["fields"]


def test_the_conflicting_edit_never_overwrites_a_newer_value_of_the_same_field(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    cid = case["id"]
    at = start(rec, "Szczegóły przypadku", cid)
    at.text_area(key="edit_quantity").set_value("5 pcs").run()
    rec._http.patch(f"/cases/{cid}", json={"expected_version": 1, "corrections": {"quantity": ["99 pcs"]}})
    button(at, "btn_save_edit").click().run()
    server = rec._http.get(f"/cases/{cid}").json()
    assert [v["value"] for f in server["fields"] if f["name"] == "quantity" for v in f["values"]] == ["99 pcs"]
    assert at.text_area(key="edit_quantity").value == "5 pcs"  # the user decides what to do next


def test_stale_banner_when_the_case_changed_since_it_was_opened(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    cid = case["id"]
    at = start(rec, "Szczegóły przypadku", cid)
    rec._http.patch(f"/cases/{cid}", json={"expected_version": 1, "note": "colleague"})
    at.run()
    assert any("zmienił się od chwili" in v(w) for w in at.warning)
    button(at, "btn_accept_current").click().run()
    assert not any("zmienił się od chwili" in v(w) for w in at.warning)


def test_stale_approval_is_refused_and_nothing_is_approved(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    cid = case["id"]
    at = start(rec, "Szczegóły przypadku", cid)
    button(at, "btn_approve").click().run()
    rec._http.patch(f"/cases/{cid}", json={"expected_version": 1, "note": "colleague changed something"})
    button(at, "btn_confirm_approve").click().run()
    no_exception(at)
    assert rec.body_of("POST", f"/cases/{cid}/approve")["expected_version"] == 1
    assert any("zmieniony" in v(w) for w in at.warning)
    assert rec._http.get(f"/cases/{cid}").json()["case"]["status"] == "ready_for_review"
    assert "btn_confirm_approve" not in keys(at, "button")  # the confirmation was dropped: look again first


# --- decisions ------------------------------------------------------------------------------------------------------------


def test_approval_needs_explicit_confirmation_and_shows_what_is_approved(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku", case["id"])
    button(at, "btn_approve").click().run()
    assert any("Potwierdź zatwierdzenie przypadku #1" in v(m) for m in at.markdown)
    summary = texts(at)
    assert "Kategoria: Reklamacja" in summary and any(t.startswith("Numer referencyjny: FV/2026/0042") for t in summary)
    at.text_area(key="confirm_note").set_value("Checked in the ERP.")
    button(at, "btn_confirm_approve").click().run()
    assert rec.body_of("POST", "/cases/1/approve") == {"expected_version": 1, "note": "Checked in the ERP."}
    assert metrics(at)["Status"].endswith("Zatwierdzony")
    assert any("zatwierdzony przez człowieka" in v(s).lower() or "ZATWIERDZONY" in v(s) for s in at.success)
    section(at, HISTORY)
    audit = at.dataframe[-1].value
    assert list(audit["Zdarzenie"])[-1] == "ZATWIERDZONY" and "human (local_demo_user)" in list(audit["Aktor"])


def test_cancelling_a_decision_changes_nothing(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku", case["id"])
    button(at, "btn_approve").click().run()
    button(at, "btn_cancel_decision").click().run()
    assert rec.writes == [] and "btn_approve" in keys(at, "button") and "btn_confirm_approve" not in keys(at, "button")


def test_approval_with_problems_needs_a_written_reason_and_acknowledgement(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    button(at, "btn_approve").click().run()
    assert any("nierozwiązane problemy" in v(w) for w in at.warning)  # not silently overridden
    confirm = lambda: button(at, "btn_confirm_approve")  # noqa: E731
    assert confirm().disabled
    at.text_area(key="confirm_override").set_value("too short").run()
    assert confirm().disabled
    at.text_area(key="confirm_override").set_value("Customer confirmed the date by phone.").run()
    assert confirm().disabled  # still needs the explicit acknowledgement
    at.checkbox(key="confirm_ack").check().run()
    assert not confirm().disabled
    confirm().click().run()
    body = rec.body_of("POST", f"/cases/{case['id']}/approve")
    assert body == {"expected_version": 1, "override_reason": "Customer confirmed the date by phone."}
    assert metrics(at)["Status"].endswith("Zatwierdzony")
    review = rec._http.get(f"/cases/{case['id']}/history").json()["reviews"][-1]
    assert review["override_reason"] == "Customer confirmed the date by phone."


def test_a_clean_case_is_approved_without_inventing_an_override(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku", case["id"])
    button(at, "btn_approve").click().run()
    assert "confirm_override" not in keys(at, "text_area")
    button(at, "btn_confirm_approve").click().run()
    assert "override_reason" not in rec.body_of("POST", f"/cases/{case['id']}/approve")


def test_server_refusal_is_displayed_and_asks_for_an_override(rec, monkeypatch):
    case = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku", case["id"])

    def refuse(*a, **kw):
        raise C.ApprovalBlocked(422, "approval_blocked", "blocked",
                                {"problems": [{"code": "x", "message": "The reference looks wrong"}]})

    monkeypatch.setattr(rec, "approve", refuse)
    button(at, "btn_approve").click().run()
    button(at, "btn_confirm_approve").click().run()
    no_exception(at)
    err = " ".join(v(e) for e in at.error)
    assert "Serwer odmówił zatwierdzenia" in err and "The reference looks wrong" in err.replace("\\", "")
    assert "confirm_override" in keys(at, "text_area")  # the override field appears; nothing was approved silently
    assert rec._http.get(f"/cases/{case['id']}").json()["case"]["status"] == "ready_for_review"


def test_rejection_needs_a_reason_and_confirmation(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    button(at, "btn_reject").click().run()
    assert button(at, "btn_confirm_reject").disabled
    at.text_area(key="confirm_reason").set_value("Duplicate of an order we already have.").run()
    assert not button(at, "btn_confirm_reject").disabled
    assert rec.writes == []
    button(at, "btn_confirm_reject").click().run()
    assert rec.body_of("POST", f"/cases/{case['id']}/reject") == {
        "expected_version": 1, "reason": "Duplicate of an order we already have.",
    }
    assert metrics(at)["Status"].endswith("Odrzucony") and any("ODRZUCONY" in v(e) for e in at.error)


def test_reopening_a_decided_case(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku", case["id"])
    button(at, "btn_approve").click().run()
    button(at, "btn_confirm_approve").click().run()
    assert "btn_reopen" in keys(at, "button") and "btn_approve" not in keys(at, "button")
    button(at, "btn_reopen").click().run()
    assert button(at, "btn_confirm_reopen").disabled
    at.text_area(key="confirm_reason").set_value("Customer sent a corrected invoice.").run()
    button(at, "btn_confirm_reopen").click().run()
    assert metrics(at)["Status"].endswith("Gotowy do przeglądu") and metrics(at)["Wersja"] == "3"
    assert "btn_save_edit" in keys(at, "button")  # editable again
    reviews = [r["action"] for r in rec._http.get(f"/cases/{case['id']}/history").json()["reviews"]]
    assert reviews == ["approve", "reopen"]


def test_decision_buttons_follow_the_servers_allowed_actions(rec):
    case = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku", case["id"])
    assert {"btn_approve", "btn_reject"} <= set(keys(at, "button")) and "btn_reopen" not in keys(at, "button")
    rec.reject(case["id"], 1, "spam")
    at = start(rec, "Szczegóły przypadku", case["id"])
    assert "btn_reopen" in keys(at, "button") and "btn_approve" not in keys(at, "button")


# --- backend problems --------------------------------------------------------------------------------------------------------


def dead_client() -> ApiClient:
    def refuse(request):
        raise httpx.ConnectError("refused")

    return ApiClient("http://127.0.0.1:8000", http=httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(refuse)))


def test_unavailable_backend_is_explained_on_every_screen(rec):
    at = start(dead_client())
    no_exception(at)
    assert any("Backend jest niedostępny" in v(e) for e in at.error)
    assert any("ai_ops_hub.api" in c.value for c in at.code)
    for page in ("Import", "Przypadki", "Wiadomości", "Szczegóły przypadku"):
        goto(at, page, 1)
        no_exception(at)
        assert any("Backend jest niedostępny" in v(e) for e in at.error), page
    assert not at.metric


def test_backend_dying_during_a_decision_is_reported_without_retrying(rec, monkeypatch):
    case = case_with(rec, CLEAN_COMPLAINT)
    at = start(rec, "Szczegóły przypadku", case["id"])
    attempts = []

    def die(*a, **kw):
        attempts.append(1)
        raise C.BackendUnavailable("The API did not answer in time.", outcome_unknown=True)

    monkeypatch.setattr(rec, "approve", die)
    button(at, "btn_approve").click().run()
    button(at, "btn_confirm_approve").click().run()
    at.run()
    no_exception(at)
    assert len(attempts) == 1
    assert any("Nie wiadomo, czy operacja została wykonana" in v(e) for e in at.error)
    assert rec._http.get(f"/cases/{case['id']}").json()["case"]["status"] == "ready_for_review"


def test_a_non_local_api_address_is_refused(monkeypatch):
    monkeypatch.setenv("AIOPS_API_URL", "http://example.com:8000")
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    no_exception(at)
    assert any("Nieprawidłowy adres API" in v(e) for e in at.error)


# --- hostile email content ------------------------------------------------------------------------------------------------------


def hostile_case(rec) -> dict:
    s, p, i, link = EVIL
    text = (
        f"From: {s} <attacker@evil.example>\nSubject: {p} {s}\nMessage-ID: <evil@x.example>\n\n"
        f"Please send us the invoice FV/2026/0666 {i} {link}\n"
        f"Customer: {s}\nAmount 100 PLN, deliver by 15/12. {p}\n"
    )
    return case_with(rec, text)


def assert_nothing_active(at: AppTest) -> None:
    joined = "\n".join(md_values(at))
    for payload in EVIL:
        assert payload not in joined, f"raw payload reached a markdown-rendering element: {payload!r}"
    assert not list(at.html), "no HTML element may be produced from email content"


def test_hostile_email_is_never_rendered_as_markup(rec):
    case = hostile_case(rec)
    at = start(rec, "Panel")
    for page in ("Panel", "Przypadki", "Wiadomości"):
        goto(at, page)
        no_exception(at)
        assert_nothing_active(at)
    goto(at, "Szczegóły przypadku", case["id"])
    for name in (ANALYSIS, HISTORY, ORIGINAL):  # every section of the detail screen
        section(at, name)
        no_exception(at)
        assert_nothing_active(at)
    # the content IS shown, as plain text
    assert EVIL[0] in at.text_area(key=f"body_{case['id']}").value
    assert any(EVIL[0] in t or EVIL[1] in t for t in texts(at))


def test_hostile_email_after_a_save_and_a_decision_prompt(rec):
    case = hostile_case(rec)
    at = start(rec, "Szczegóły przypadku", case["id"])
    at.text_area(key="edit_product").set_value(EVIL[1]).run()
    at.text_area(key="edit_note").set_value(EVIL[2]).run()
    button(at, "btn_save_edit").click().run()
    no_exception(at)
    assert_nothing_active(at)
    button(at, "btn_approve").click().run()
    assert_nothing_active(at)
    assert any(EVIL[1] in t for t in texts(at))  # the approval summary shows it as text


def test_server_error_messages_that_echo_hostile_input_are_escaped(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    at = start(rec, "Szczegóły przypadku", case["id"])
    at.text_area(key="edit_deadline").set_value(EVIL[0]).run()
    button(at, "btn_save_edit").click().run()
    raw = " ".join(str(e.value) for e in at.error)  # exactly what reaches the markdown renderer
    assert at.error and "<script>" not in raw and r"\<script\>" in raw  # present, but escaped
    assert_nothing_active(at)


def test_hostile_import_feedback_is_escaped(rec):
    at = start(rec, "Import")
    at.file_uploader(key="eml_upload_0").upload("x.eml", f"From: a@x.example\nSubject: {EVIL[0]}\nMessage-ID: <h@x.example>\n\nbody".encode()).run()
    button(at, "btn_import_file").click().run()
    assert_nothing_active(at)


# --- regressions found by mutation testing ---------------------------------------------------------------------------------


def test_changing_a_filter_returns_to_page_one_even_when_more_pages_remain(rec):
    make_cases(rec, 25)  # all ready_for_review: three pages of 10 before and after filtering
    at = start(rec, "Przypadki")
    at.selectbox(key="f_size").select(10).run()
    button(at, "cases_next").click().run()
    assert at.session_state["f_page"] == 2
    at.selectbox(key="f_status").select("ready_for_review").run()
    assert at.session_state["f_page"] == 1  # not merely clamped: the filter change resets it
    button(at, "cases_next").click().run()
    at.text_input(key="f_q").set_value("report").run()
    assert at.session_state["f_page"] == 1


def hostile_detail(rec, case_id: int):
    """What a (compromised or buggy) server could send: markup inside problem and warning texts."""
    from ai_ops_hub.ui import models as m

    real = rec.get_case(case_id)
    payload = " ".join(EVIL)
    fields = [f.model_copy(update={"values": [v.model_copy(update={"value": payload}) for v in f.values]}) if f.values else f
              for f in real.fields]
    return real.model_copy(update={
        "problems": [m.Problem(code="x", message=f"deadline is {payload}", field="deadline")],
        "fields": fields,
        "case": real.case.model_copy(update={"subject": payload}),
    })


def test_markup_inside_server_supplied_problem_texts_is_escaped(rec, monkeypatch):
    case = case_with(rec, AMBIGUOUS_ORDER)
    fake = hostile_detail(rec, case["id"])
    monkeypatch.setattr(rec, "get_case", lambda _id: fake)
    at = start(rec, "Szczegóły przypadku", case["id"])
    no_exception(at)
    assert any("deadline is" in v(w) for w in at.warning)  # the problem IS displayed...
    assert_nothing_active(at)  # ...but nothing reaches a markdown renderer unescaped
    button(at, "btn_approve").click().run()
    assert_nothing_active(at)


def test_markup_inside_a_server_error_message_on_import_is_escaped(rec, monkeypatch):
    def boom(_text):
        raise C.ApiError(422, "malformed_message", " ".join(EVIL))

    monkeypatch.setattr(rec, "import_text", boom)
    at = start(rec, "Import")
    at.text_area(key="import_text_0").set_value("Subject: x\n\nbody").run()
    button(at, "btn_import_text").click().run()
    assert at.error and any("malformed_message" in v(e) for e in at.error)
    assert_nothing_active(at)


def test_only_the_selected_detail_section_is_fetched(rec):
    case = case_with(rec, AMBIGUOUS_ORDER)
    rec.calls.clear()
    at = start(rec, "Szczegóły przypadku", case["id"])
    assert not [c for c in rec.calls if c[1].endswith(("/history", "/audit"))]
    section(at, HISTORY)
    assert [c for c in rec.calls if c[1].endswith("/history")] and [c for c in rec.calls if c[1].endswith("/audit")]
