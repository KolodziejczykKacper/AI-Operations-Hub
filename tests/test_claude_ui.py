"""The Claude analysis screen (AppTest against the real API with a FAKE provider) and the client.

Proves the UI rules: nothing is sent without a fresh explicit consent in the same run, reruns and
navigation never call Claude, failures create nothing, costs and methods are shown honestly.
No network, no API key.
"""

from __future__ import annotations

from pathlib import Path

import httpx2 as httpx
import pytest
from claude_helpers import (
    FAKE_KEY,
    GOOD,
    FakeClaude,
    analyze_claude,
    answer,
    api_error,
    claude_client,
    enabled_settings,
    raw_field,
    response,
)
from conftest import SAMPLES, import_text
from export_helpers import complaint
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.errors import AppTestError

from ai_ops_hub.config import Settings
from ai_ops_hub.ui import client as C
from ai_ops_hub.ui.client import ApiClient

APP = Path(__file__).resolve().parents[1] / "src" / "ai_ops_hub" / "ui" / "streamlit_app.py"
PAGE = "Analiza Claude"


# --- the client (mocked transport) -----------------------------------------------------------------------------


def make(handler):
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    http = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(wrapped))
    return ApiClient("http://127.0.0.1:8000", http=http), seen


def error(status, code, message="boom"):
    return httpx.Response(status, json={"error": {"code": code, "message": message, "details": None}})


ANALYSIS = {
    "id": 5, "message_id": 1, "analyzer_name": "claude_analyzer", "analyzer_version": "1.0/claude-haiku-5-5",
    "analysis_method": "llm", "category": "complaint", "requires_review": False, "source_fingerprint": "f",
    "created_at": "2026-11-17T10:00:00Z", "result": {"fields": [], "warnings": [], "review_reasons": []},
}


def test_client_sends_the_consent_body_with_a_long_timeout_and_parses_the_result():
    body = {"created": True, "analysis": ANALYSIS, "usage": None, "case_id": None, "note": "n"}
    c, seen = make(lambda r: httpx.Response(201, json=body))
    out = c.claude_analyze(1, model="claude-haiku-5-5", accepted_max_cost_usd=0.002, content_fingerprint="abcdef0123456789")
    assert out.created and out.analysis.analysis_method == "llm"
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/messages/1/claude/analyze"
    assert req.read() and __import__("json").loads(req.content) == {
        "message_id": 1, "content_fingerprint": "abcdef0123456789", "confirm_send_to_anthropic": True,
        "model": "claude-haiku-5-5", "accepted_max_cost_usd": 0.002}
    assert req.extensions["timeout"]["read"] == C.CLAUDE_TIMEOUT_SECONDS > 30  # longer than the server's provider timeout


def test_client_never_retries_the_paid_call_and_flags_an_unknown_outcome_on_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    c, seen = make(handler)
    with pytest.raises(C.BackendUnavailable) as info:
        c.claude_analyze(1, model="m", accepted_max_cost_usd=1, content_fingerprint="f" * 16)
    assert len(seen) == 1 and info.value.outcome_unknown is True


@pytest.mark.parametrize("status, code", [(409, "claude_disabled"), (409, "claude_not_configured"), (422, "claude_consent_required"),
                                          (422, "claude_cost_guard"), (413, "claude_input_too_large"), (429, "claude_rate_limited"),
                                          (502, "claude_bad_response"), (504, "claude_timeout")])
def test_client_maps_claude_errors_to_typed_errors_and_does_not_retry(status, code):
    c, seen = make(lambda r: error(status, code, "msg"))
    with pytest.raises(C.ApiError) as info:
        c.claude_analyze(1, model="m", accepted_max_cost_usd=1, content_fingerprint="f" * 16)
    assert (info.value.status, info.value.code) == (status, code) and len(seen) == 1


def test_client_reads_status_preview_usage_analyses_and_comparison_with_get_only():
    status = {"enabled": False, "key_configured": False, "model": "claude-haiku-5-5", "can_analyze": False, "blocked_reason": "off"}
    preview = {"message_id": 1, "enabled": True, "key_configured": True, "model": "m", "subject_chars": 3, "body_chars": 4,
               "content_fingerprint": "abcdef0123456789", "estimated_input_tokens": 1600, "max_output_tokens": 4000, "estimated_max_cost_usd": 0.002,
               "already_analyzed": False, "can_send": True}
    cmp_ = {"message_id": 1, "analysis_a_id": 1, "analysis_b_id": 2, "method_a": "rule_based", "method_b": "llm", "version_a": "1",
            "version_b": "2", "category_a": "order", "category_b": "order", "category_match": True, "requires_review_a": False,
            "requires_review_b": False, "warning_count_a": 0, "warning_count_b": 0, "same": 0, "different": 0, "only_a": 0, "only_b": 0}
    routes = {"/claude/status": status, "/messages/1/claude/preview": preview, "/messages/1/claude/usage": [],
              "/messages/1/analyses": [ANALYSIS], "/messages/1/compare": cmp_}
    c, seen = make(lambda r: httpx.Response(200, json=routes[r.url.path]))
    assert c.claude_status().enabled is False and c.claude_preview(1).can_send and c.claude_usage(1) == []
    assert c.list_analyses(1)[0].id == 5 and c.compare_analyses(1, 1, 2).category_match
    assert {r.method for r in seen} == {"GET"} and dict(seen[-1].url.params) == {"analysis_a": "1", "analysis_b": "2"}


def test_client_creates_a_case_from_an_explicit_analysis():
    summary = {"id": 3, "message_id": 1, "analysis_id": 5, "status": "ready_for_review", "version": 1,
               "created_at": "2026-11-17T10:00:00Z", "updated_at": "2026-11-17T10:00:00Z"}
    c, seen = make(lambda r: httpx.Response(201, json=summary))
    assert c.create_case(1, 5).id == 3
    assert seen[0].url.path == "/messages/1/case" and __import__("json").loads(seen[0].content) == {"analysis_id": 5}


def test_the_ui_package_still_has_no_direct_provider_or_database_access():
    import re

    root = Path(__file__).resolve().parents[1] / "src" / "ai_ops_hub" / "ui"
    forbidden = re.compile(r"^\s*(?:from|import)\s+(anthropic|openpyxl|sqlalchemy|sqlmodel|sqlite3|ai_ops_hub\.(?:db|services|export|cases|api|analysis))\b", re.M)
    for path in root.rglob("*.py"):
        assert not forbidden.search(path.read_text(encoding="utf-8")), path
    for path in root.rglob("*.py"):
        assert FAKE_KEY not in path.read_text(encoding="utf-8") and "ANTHROPIC_API_KEY" not in path.read_text(encoding="utf-8").replace(
            "`ANTHROPIC_API_KEY`", "")


# --- the screen (AppTest) -----------------------------------------------------------------------------------------


class Rec(ApiClient):
    def __init__(self, http) -> None:
        super().__init__("http://127.0.0.1:8000", http=http)
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, dict | None]] = []

    def _send(self, method, path, **kw):
        self.calls.append((method, path))
        self.bodies.append((path, kw.get("json_body")))
        return super()._send(method, path, **kw)

    def n(self, fragment: str, method: str | None = None) -> int:
        return sum(1 for m, p in self.calls if fragment in p and (method is None or m == method))

    @property
    def paid(self) -> int:
        return self.n("/claude/analyze", "POST")


def start(rec, page=PAGE, message=1) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.session_state["client"] = rec
    at.session_state["page"] = page
    at.session_state["cl_message"] = message
    return at.run()


def shown(at) -> str:
    out = []
    for kind in ("markdown", "caption", "success", "info", "warning", "error", "header", "subheader", "text"):
        out += [str(e.value) for e in getattr(at, kind)]
    out += [str(m.label) + " " + str(m.value) for m in at.metric]
    out += [c.label for c in at.checkbox] + [b.label for b in at.button]
    return "\n".join(out).replace("\\", "")


def tick(at, n=0, mid=1):
    return at.checkbox(key=f"cl_confirm_{mid}_{n}").check().run()


def send(at):
    return at.button(key="claude_send").click().run()


@pytest.fixture()
def env(engine):
    """(enabled client, fake provider, Rec) with one imported message (#1)."""
    fake = FakeClaude(response(GOOD))
    with claude_client(engine, enabled_settings(), fake) as (c, _f, factory):
        mid = import_text(c, complaint(1))
        assert mid == 1
        yield c, fake, Rec(c), engine, factory


def test_the_screen_is_in_the_navigation_and_describes_rule_based_as_the_default(env):
    c, fake, rec, _e, _fac = env
    at = start(rec)
    assert not at.exception and PAGE in at.sidebar.radio[0].options
    assert at.header[0].value == "Analiza Claude (opcjonalna)"
    text = shown(at)
    assert "rule_based" in text and "płatne" in text and "nigdy nie wysyłają danych" in text
    assert fake.calls == 0


def test_when_disabled_the_screen_explains_how_to_enable_it_and_offers_no_send_button(engine):
    fake = FakeClaude(response(GOOD))
    with claude_client(engine, Settings(_env_file=None), fake) as (c, _f, factory):
        import_text(c, complaint(1))
        rec = Rec(c)
        at = start(rec)
        text = shown(at)
        assert "AIOPS_CLAUDE_ENABLED=true" in text and "ANTHROPIC_API_KEY" in text and "Tryb offline" in text
        assert "claude_send" not in [b.key for b in at.button] and fake.calls == 0 and rec.paid == 0 and factory.built == []


def test_the_preview_shows_the_privacy_warning_the_model_and_the_estimated_cost(env):
    _c, fake, rec, _e, _fac = env
    at = start(rec)
    text = shown(at)
    assert "zostanie przesłana do Anthropic" in text and "claude-haiku-5-5" in text
    assert "Maks. koszt (szac.)" in text and "Szac. tokeny wejścia" in text and "$0.00" in text
    assert "Nie są wysyłane: adres nadawcy" in text and "The subject and body" not in text  # Polish UI, not server English
    assert "limit rozliczeń" in text and "nie gwarantowany limit wydatków" in text
    assert at.button(key="claude_send").disabled is True  # consent first
    assert fake.calls == 0


def test_nothing_is_sent_on_load_reruns_or_navigation(env):
    _c, fake, rec, _e, _fac = env
    at = start(rec)
    for _ in range(3):
        at.run()
    at.session_state["page"] = "Panel"
    at.run()
    at.session_state["page"] = PAGE
    at.run()
    assert rec.paid == 0 and fake.calls == 0


def test_without_ticking_the_consent_box_the_button_does_nothing(env):
    _c, fake, rec, _e, _fac = env
    at = start(rec)
    assert at.button(key="claude_send").disabled is True
    with pytest.raises(AppTestError, match="disabled"):  # a browser user cannot click it either
        send(at)
    assert rec.paid == 0 and fake.calls == 0


def test_ticking_alone_sends_nothing_only_the_click_does(env):
    _c, fake, rec, _e, _fac = env
    at = tick(start(rec))
    assert at.button(key="claude_send").disabled is False and rec.paid == 0 and fake.calls == 0


def test_consent_plus_click_sends_exactly_one_request_with_the_previewed_model_and_cost(env):
    _c, fake, rec, engine, factory = env
    at = send(tick(start(rec)))
    assert not at.exception and rec.paid == 1 and fake.calls == 1
    body = next(b for p, b in rec.bodies if p.endswith("/claude/analyze"))
    assert body["confirm_send_to_anthropic"] is True and body["model"] == "claude-haiku-5-5"
    assert 0 < body["accepted_max_cost_usd"] < 0.01
    text = shown(at)
    assert "Zapisano analizę Claude #1" in text and "Koszt rzeczywisty (z użycia)" in text and "Opóźnienie" in text
    assert "Odrzucone wartości" in text and "Nie utworzono przypadku" in text


def test_reruns_after_a_successful_request_never_send_again(env):
    _c, fake, rec, _e, _fac = env
    at = send(tick(start(rec)))
    for _ in range(4):
        at.run()
    assert rec.paid == 1 and fake.calls == 1
    assert "ma już analizę Claude" in shown(at) and "claude_send" not in [b.key for b in at.button]  # nothing left to send


def test_consent_does_not_carry_over_to_the_next_message(engine):
    fake = FakeClaude(response(GOOD))
    with claude_client(engine, enabled_settings(), fake) as (c, _f, _fac):
        import_text(c, complaint(1))
        import_text(c, complaint(2))
        rec = Rec(c)
        at = send(tick(start(rec)))
        assert rec.paid == 1
        at.number_input(key="cl_message").set_value(2).run()
        assert at.checkbox(key="cl_confirm_2_1").value is False and at.button(key="claude_send").disabled is True
        with pytest.raises(AppTestError, match="disabled"):
            send(at)
        assert rec.paid == 1  # no second request without a fresh tick


def test_a_provider_failure_shows_an_error_and_stores_no_analysis(engine):
    fake = FakeClaude(api_error("timeout"))
    with claude_client(engine, enabled_settings(), fake) as (c, _f, _fac):
        import_text(c, complaint(1))
        rec = Rec(c)
        at = send(tick(start(rec)))
        assert not at.exception
        text = shown(at)
        assert "nie powiodło się" in text and "Żadna analiza nie została zapisana" in text and "claude_timeout" in text
        assert "Zapisano analizę Claude" not in text and "secret provider detail" not in text
        assert c.get("/messages/1/analyses").json() == []
        assert fake.calls == 1 and rec.paid == 1
        # the failed (possibly billed) request is visible in the usage table
        assert "Zapisane żądania do Claude (1)" in " ".join(e.label for e in at.expander)
        send_again = at.checkbox(key="cl_confirm_1_1")
        assert send_again.value is False  # a retry needs a new, explicit consent


def test_an_unknown_outcome_is_explained_honestly(engine):
    fake = FakeClaude(response(GOOD))
    with claude_client(engine, enabled_settings(), fake) as (c, _f, _fac):
        import_text(c, complaint(1))
        rec = Rec(c)
        at = start(rec)
        real = rec._send

        def cut(method, path, **kw):
            if path.endswith("/claude/analyze"):
                raise C.BackendUnavailable("The API did not answer in time.", outcome_unknown=True)
            return real(method, path, **kw)

        rec._send = cut
        at = send(tick(at))
        assert "Nie wiadomo, czy żądanie dotarło do Claude" in shown(at) and "Mogło zostać naliczone" in shown(at)


@pytest.mark.parametrize("settings_kw, fragment", [
    ({"claude_max_input_chars": 100}, "limit for Claude analysis is 100"),
    ({"claude_max_cost_usd_per_request": 0.0001}, "per-request limit"),
])
def test_blocked_requests_show_the_reason_and_no_send_button(engine, settings_kw, fragment):
    fake = FakeClaude(response(GOOD))
    with claude_client(engine, enabled_settings(**settings_kw), fake) as (c, _f, _fac):
        import_text(c, complaint(1))
        rec = Rec(c)
        at = start(rec)
        assert fragment in shown(at) and "claude_send" not in [b.key for b in at.button] and fake.calls == 0


def test_a_conflicting_import_is_never_offered_to_claude(engine):
    fake = FakeClaude(response(GOOD))
    with claude_client(engine, enabled_settings(), fake) as (c, _f, _fac):
        c.post("/messages/eml", content=(SAMPLES / "01_invoice_dispute_pl.eml").read_bytes(), headers={"Content-Type": "message/rfc822"})
        conflict = c.post("/messages/eml", content=(SAMPLES / "02_invoice_dispute_pl_conflict.eml").read_bytes(), headers={"Content-Type": "message/rfc822"})
        cid = conflict.json()["message"]["id"]
        rec = Rec(c)
        at = start(rec, message=cid)
        assert "KONFLIKT" in shown(at) and rec.n("/claude/preview") == 0 and "claude_send" not in [b.key for b in at.button]


def test_an_unknown_message_is_reported_without_a_crash(env):
    _c, _f, rec, _e, _fac = env
    at = start(rec, message=999)
    assert not at.exception and "Nie ma wiadomości #999" in shown(at)


def test_import_and_rule_based_analysis_through_the_ui_never_call_claude(engine):
    fake = FakeClaude(response(GOOD))
    with claude_client(engine, enabled_settings(), fake) as (c, _f, factory):
        rec = Rec(c)
        at = AppTest.from_file(str(APP), default_timeout=30)
        at.session_state["client"] = rec
        at.session_state["page"] = "Import"
        at.run()
        at.text_area(key="import_text_0").set_value(complaint(1)).run()
        at.button(key="btn_import_text").click().run()
        assert rec.n("/messages/text", "POST") == 1 and rec.n("/analyze", "POST") >= 1
        assert rec.paid == 0 and rec.n("/claude") == 0 and fake.calls == 0 and factory.built == []


# --- comparison, cases and honest labels ---------------------------------------------------------------------------------


def analysed_both(c, mid=1, claude_answer=None):
    c.post(f"/messages/{mid}/analyze")
    analyze_claude(c, mid)


def test_the_comparison_shows_both_methods_the_models_and_field_verdicts(engine):
    different = answer("order", [raw_field("reference", "FV/2026/0001"), raw_field("amount", "12 500,00 zł", "12500.00 PLN"),
                                 raw_field("product", "invented thing")])
    fake = FakeClaude(response(different))
    with claude_client(engine, enabled_settings(), fake) as (c, _f, _fac):
        import_text(c, complaint(1))
        analysed_both(c)
        at = start(Rec(c))
        assert not at.exception
        text = shown(at)
        assert "Porównanie analiz (tylko do odczytu)" in text and "Kategoria rule_based" in text and "Kategoria Claude" in text
        assert "Analizatory wybrały różne kategorie" in text and "nie są skalibrowane" in text
        frames = [df.value for df in at.dataframe]
        flat = " ".join(str(v) for f in frames for v in f.to_numpy().ravel())
        assert "rule_based (reguły, offline, bez AI)" in flat and "Claude API (model językowy, płatne)" in flat
        assert "1.0/claude-haiku-5-5" in flat and "zgodne" in flat and "tylko rule_based" in flat
        # the hallucinated product is listed as locally rejected, not as a result
        assert "odrzucone lokalnie (1)" in " ".join(e.label for e in at.expander)
        assert "invented thing" not in flat


def test_the_comparison_needs_both_analyses(env):
    c, _f, rec, _e, _fac = env
    c.post("/messages/1/analyze")
    assert "Porównanie wymaga obu analiz" in shown(start(rec))


def test_an_existing_case_keeps_its_analysis_and_the_screen_says_so(env):
    c, _f, rec, _e, _fac = env
    analysed_both(c)
    at = start(rec)
    text = shown(at)
    assert "Przypadek #1" in text and "zachowuje swoją analizę" in text and "rule_based (reguły, offline, bez AI)" in text
    assert "claude_create_case" not in [b.key for b in at.button] and "claude_open_case" in [b.key for b in at.button]
    assert c.get("/cases/1").json()["analysis"]["analysis_method"] == "rule_based"


def test_a_case_can_be_created_from_the_chosen_claude_analysis_and_is_labelled_llm(env):
    c, _f, rec, _e, _fac = env
    analyze_claude(c, 1)
    at = start(rec)
    pick = at.selectbox(key="cl_case_pick_1")
    assert len(pick.options) == 1 and "Claude API" in pick.options[0]
    assert "nigdy nie jest tworzony automatycznie" in shown(at)
    at.button(key="claude_create_case").click().run()
    assert rec.n("/messages/1/case", "POST") == 1 and not at.exception
    assert at.session_state["page"] == "Szczegóły przypadku"
    text = shown(at)
    assert "llm: Claude API" in text and "To model językowy, nie reguły" in text
    assert "nie jest** model AI" not in text  # the rule-based disclaimer must not describe a Claude analysis
    assert c.get("/cases/1").json()["analysis"]["analysis_method"] == "llm"
    assert c.get("/cases/1").json()["case"]["status"] in ("needs_review", "ready_for_review")  # never auto-approved


def test_the_case_detail_names_the_real_method_in_its_table_header(env):
    c, _f, rec, _e, _fac = env
    analyze_claude(c, 1)
    c.post("/messages/1/case", json={"analysis_id": 1})
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.session_state["client"] = rec
    at.session_state["page"] = "Szczegóły przypadku"
    at.session_state["selected_case"] = 1
    at.run()
    at.radio(key="detail_section").set_value("2 · Analiza i poprawki").run()
    columns = " ".join(str(col) for df in at.dataframe for col in df.value.columns)
    assert "Wartość z analizy (Claude, llm)" in columns and "(rule_based)" not in columns


def test_the_rule_based_case_detail_is_unchanged(env):
    c, _f, rec, _e, _fac = env
    c.post("/messages/1/analyze")
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.session_state["client"] = rec
    at.session_state["page"] = "Szczegóły przypadku"
    at.session_state["selected_case"] = 1
    at.run()
    assert "model AI" in shown(at) and "Claude" not in shown(at)


def test_the_audit_trail_labels_a_claude_analysis_event_correctly(env):
    c, _f, rec, _e, _fac = env
    analyze_claude(c, 1)
    c.post("/messages/1/case", json={"analysis_id": 1})
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.session_state["client"] = rec
    at.session_state["page"] = "Szczegóły przypadku"
    at.session_state["selected_case"] = 1
    at.run()
    at.radio(key="detail_section").set_value("3 · Historia i audyt").run()
    cells = " ".join(str(v) for df in at.dataframe for v in df.value.to_numpy().ravel())
    assert "Analiza wykonana (Claude, llm)" in cells


def test_hostile_email_text_is_shown_as_plain_text_on_this_screen(engine):
    mail = ('From: x@zielonadolina.example\nSubject: <script>alert(1)</script> [x](javascript:alert(1))\n'
            "Message-ID: <hostile-cl@zielonadolina.example>\n\nPlease send FV/2026/0100.\n")
    fake = FakeClaude(response(answer("complaint", [raw_field("reference", "FV/2026/0100")])))
    with claude_client(engine, enabled_settings(), fake) as (c, _f, _fac):
        mid = import_text(c, mail)
        analyze_claude(c, mid)
        at = start(Rec(c), message=mid)
        assert not at.exception
        assert any("<script>" in str(t.value) for t in at.text)  # shown via st.text (literal)
        for kind in ("markdown", "caption", "error", "warning", "info", "success"):
            assert not any("<script" in str(e.value) for e in getattr(at, kind))
