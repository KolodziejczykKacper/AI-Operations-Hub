"""HTTP API integration and security tests (TestClient, temporary databases, no network)."""

from __future__ import annotations

import json

import pytest
from conftest import (
    AMBIGUOUS_ORDER,
    CLEAN_COMPLAINT,
    EML,
    INJECTION_TEXT,
    OTHER_TEXT,
    SAMPLES,
    import_text,
    open_case,
)


def error_code(response) -> str:
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) >= {"code", "message"}
    return body["error"]["code"]


def eml(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


# --- basics ----------------------------------------------------------------------------


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert "local_demo_user" in r.json()["auth"]


def test_swagger_ui_and_openapi_are_served(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "rule-based" in spec["info"]["description"] and "not AI" in spec["info"]["description"]
    paths = set(spec["paths"])
    assert {
        "/health", "/messages/eml", "/messages/text", "/messages", "/messages/{message_id}",
        "/messages/{message_id}/analyze", "/messages/{message_id}/analysis", "/messages/{message_id}/audit",
        "/cases", "/cases/{case_id}", "/cases/{case_id}/approve", "/cases/{case_id}/reject",
        "/cases/{case_id}/reopen", "/cases/{case_id}/history", "/cases/{case_id}/audit",
    } <= paths


def test_the_api_has_no_endpoint_that_sends_email_or_acts_externally(client):
    paths = " ".join(client.get("/openapi.json").json()["paths"]).lower()
    for forbidden in ("send", "mail/", "export", "execute", "webhook", "auto"):
        assert forbidden not in paths


def test_unknown_route_uses_the_error_envelope(client):
    r = client.get("/nope")
    assert r.status_code == 404 and error_code(r) == "not_found"


def test_wrong_method_uses_the_error_envelope(client):
    r = client.delete("/cases/1")
    assert r.status_code == 405 and error_code(r) == "method_not_allowed"


# --- import --------------------------------------------------------------------------------


def test_import_eml_new_duplicate_and_conflict_status_codes(client):
    first = client.post("/messages/eml", content=eml("01_invoice_dispute_pl.eml"), headers=EML)
    assert first.status_code == 201 and first.json()["decision"] == "new"
    mid = first.json()["message"]["id"]

    dup = client.post("/messages/eml", content=eml("01_invoice_dispute_pl.eml"), headers=EML)
    assert dup.status_code == 200 and dup.json()["decision"] == "duplicate"
    assert dup.json()["message"]["id"] == mid

    conflict = client.post("/messages/eml", content=eml("02_invoice_dispute_pl_conflict.eml"), headers=EML)
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["decision"] == "conflict" and body["message"]["import_status"] == "conflict"
    assert body["existing_message"]["id"] == mid  # the canonical record is untouched


def test_a_conflicting_import_is_stored_not_lost(client):
    client.post("/messages/eml", content=eml("01_invoice_dispute_pl.eml"), headers=EML)
    client.post("/messages/eml", content=eml("02_invoice_dispute_pl_conflict.eml"), headers=EML)
    page = client.get("/messages").json()
    assert page["total"] == 2
    assert sorted(m["import_status"] for m in page["items"]) == ["conflict", "imported"]


def test_import_text(client):
    r = client.post("/messages/text", json={"text": CLEAN_COMPLAINT})
    assert r.status_code == 201 and r.json()["message"]["source_kind"] == "pasted_text"
    again = client.post("/messages/text", json={"text": CLEAN_COMPLAINT})
    assert again.status_code == 200 and again.json()["decision"] == "duplicate"


def test_import_audit_event_is_written(client):
    mid = import_text(client, CLEAN_COMPLAINT)
    events = client.get(f"/messages/{mid}/audit").json()
    assert [e["event_type"] for e in events] == ["message_imported"]


@pytest.mark.parametrize("ctype", ["text/plain", "application/json", "multipart/form-data", ""])
def test_eml_import_refuses_cross_site_friendly_content_types(client, ctype):
    headers = {"Content-Type": ctype} if ctype else {}
    r = client.post("/messages/eml", content=eml("01_invoice_dispute_pl.eml"), headers=headers)
    assert r.status_code == 415 and error_code(r) == "unsupported_media_type"
    assert client.get("/messages").json()["total"] == 0


def test_eml_import_accepts_octet_stream(client):
    r = client.post(
        "/messages/eml", content=eml("01_invoice_dispute_pl.eml"),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 201


def test_empty_eml_is_rejected(client):
    r = client.post("/messages/eml", content=b"", headers=EML)
    assert r.status_code == 422 and error_code(r) == "empty_input"


@pytest.mark.parametrize("payload", [b"\x00\x01\x02 binary garbage", b"just some text without headers"])
def test_malformed_eml_is_rejected_cleanly(client, payload):
    r = client.post("/messages/eml", content=payload, headers=EML)
    assert r.status_code == 422 and error_code(r) == "malformed_message"
    assert client.get("/messages").json()["total"] == 0


def test_oversized_eml_is_rejected(client):
    big = b"From: a@x.example\nSubject: big\n\n" + b"x" * (6 * 1024 * 1024)
    r = client.post("/messages/eml", content=big, headers=EML)
    assert r.status_code == 413 and error_code(r) == "payload_too_large"
    assert client.get("/messages").json()["total"] == 0


def test_eml_over_the_parser_limit_but_under_the_transport_limit_is_still_413(engine, small_settings):
    from fastapi.testclient import TestClient

    from ai_ops_hub.api import create_app

    with TestClient(create_app(settings=small_settings, engine=engine)) as c:
        data = b"From: a@x.example\nSubject: s\n\n" + b"y" * 3_000  # > max_eml_bytes=2000
        r = c.post("/messages/eml", content=data, headers=EML)
        assert r.status_code == 413 and error_code(r) == "input_too_large"


def test_oversized_text_is_rejected(engine, small_settings):
    from fastapi.testclient import TestClient

    from ai_ops_hub.api import create_app

    with TestClient(create_app(settings=small_settings, engine=engine)) as c:
        r = c.post("/messages/text", json={"text": "Subject: s\n\n" + "z" * 600})
        assert r.status_code == 413 and error_code(r) == "input_too_large"


def test_chunked_upload_without_content_length_is_stopped_too(client):
    def chunks():
        for _ in range(8):
            yield b"x" * (1024 * 1024)

    r = client.post("/messages/eml", content=chunks(), headers=EML)
    assert r.status_code == 413
    assert client.get("/messages").json()["total"] == 0


def test_invalid_json_is_a_400(client):
    r = client.post("/messages/text", content="{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and error_code(r) == "invalid_json"


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": 5}, {"text": "x", "actor": "root"}, []])
def test_invalid_text_payloads_are_422(client, body):
    r = client.post("/messages/text", json=body)
    assert r.status_code == 422 and error_code(r) == "validation_error"


def test_whitespace_only_text_is_empty_input(client):
    r = client.post("/messages/text", json={"text": "   \n  "})
    assert r.status_code == 422


def test_validation_errors_do_not_echo_the_submitted_input(client):
    r = client.post("/messages/text", json={"text": 123456789})
    assert "123456789" not in r.text.replace('"type"', "")


# --- listing, pagination, details ---------------------------------------------------------------


def test_message_pagination(client):
    for i in range(5):
        import_text(client, f"From: a@x.example\nSubject: s{i}\nMessage-ID: <p{i}@x.example>\n\nbody {i}")
    page = client.get("/messages", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 5 and len(page["items"]) == 2 and page["limit"] == 2
    ids = [m["id"] for m in page["items"]]
    second = client.get("/messages", params={"limit": 2, "offset": 2}).json()
    assert [m["id"] for m in second["items"]] == [ids[-1] + 1, ids[-1] + 2]
    assert client.get("/messages", params={"offset": 99}).json()["items"] == []


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 201}, {"limit": -1}, {"offset": -1}, {"limit": "x"}]
)
@pytest.mark.parametrize("path", ["/messages", "/cases"])
def test_bad_pagination_is_422(client, path, params):
    r = client.get(path, params=params)
    assert r.status_code == 422 and error_code(r) == "validation_error"


def test_message_details_include_the_original_text(client):
    mid = import_text(client, CLEAN_COMPLAINT)
    view = client.get(f"/messages/{mid}").json()
    assert "FV/2026/0042" in view["message"]["body_text"]
    assert view["case_id"] is None and view["analysis_count"] == 0


def test_message_list_shows_case_link_after_analysis(client):
    case = open_case(client)
    item = client.get("/messages").json()["items"][0]
    assert item["case_id"] == case["id"] and item["case_status"] == case["status"]


@pytest.mark.parametrize("path", ["/messages/{}", "/messages/{}/analysis", "/messages/{}/audit", "/cases/{}",
                                  "/cases/{}/history", "/cases/{}/audit"])
def test_missing_records_are_404(client, path):
    r = client.get(path.format(9999))
    assert r.status_code == 404
    assert error_code(r) in {"message_not_found", "case_not_found"}


@pytest.mark.parametrize("bad_id", ["0", "-1", "abc", "99999999999999999999999"])
def test_invalid_ids_are_rejected_before_the_database(client, bad_id):
    for path in ("/messages/", "/cases/"):
        r = client.get(path + bad_id)
        assert r.status_code == 422 and error_code(r) == "validation_error"


# --- analysis ----------------------------------------------------------------------------------


def test_analyze_creates_analysis_and_case(client):
    mid = import_text(client, CLEAN_COMPLAINT)
    r = client.post(f"/messages/{mid}/analyze")
    assert r.status_code == 201
    body = r.json()
    assert body["analysis_created"] and body["case_created"]
    assert body["analysis"]["analysis_method"] == "rule_based"
    assert body["analysis"]["category"] == "complaint"
    assert body["case"]["status"] in {"needs_review", "ready_for_review"}
    assert body["case"]["status"] not in {"approved", "rejected"}


def test_analyze_is_idempotent(client):
    mid = import_text(client, CLEAN_COMPLAINT)
    first = client.post(f"/messages/{mid}/analyze").json()
    again = client.post(f"/messages/{mid}/analyze")
    assert again.status_code == 200
    body = again.json()
    assert not body["analysis_created"] and not body["case_created"]
    assert body["analysis"]["id"] == first["analysis"]["id"] and body["case"]["id"] == first["case"]["id"]
    assert len(client.get(f"/messages/{mid}/audit").json()) == 3  # imported, analysis, case_created


def test_analyze_refuses_a_conflicting_import_and_creates_nothing(client, engine):
    client.post("/messages/eml", content=eml("01_invoice_dispute_pl.eml"), headers=EML)
    conflict = client.post("/messages/eml", content=eml("02_invoice_dispute_pl_conflict.eml"), headers=EML)
    cid = conflict.json()["message"]["id"]
    r = client.post(f"/messages/{cid}/analyze")
    assert r.status_code == 409 and error_code(r) == "conflict_not_analyzable"
    assert client.get(f"/messages/{cid}/analysis").status_code == 404
    assert client.get("/cases").json()["total"] == 0
    assert client.get(f"/messages/{cid}").json()["case_id"] is None


def test_analyze_missing_message_is_404(client):
    r = client.post("/messages/4242/analyze")
    assert r.status_code == 404 and error_code(r) == "message_not_found"


def test_latest_analysis_exposes_fields_spans_and_warnings(client):
    mid = import_text(client, AMBIGUOUS_ORDER)
    assert client.get(f"/messages/{mid}/analysis").status_code == 404  # not analyzed yet
    client.post(f"/messages/{mid}/analyze")
    analysis = client.get(f"/messages/{mid}/analysis").json()
    body = client.get(f"/messages/{mid}").json()["message"]["body_text"]
    for f in analysis["result"]["fields"]:
        span = f["value"]["span"]
        text = body if span["source"] == "body" else None
        if text is not None:
            assert text[span["start"]:span["end"]] == span["text"]  # provenance survives the API
    deadline = next(f for f in analysis["result"]["fields"] if f["name"] == "deadline")
    assert deadline["status"] == "ambiguous" and deadline["warnings"]


def test_analysis_history_survives_a_new_analyzer_version(engine, settings, client):
    from fastapi.testclient import TestClient

    from ai_ops_hub.analysis import RuleBasedAnalyzer
    from ai_ops_hub.api import create_app

    class V2(RuleBasedAnalyzer):
        version = "2.0.0"

        def analyze(self, data):
            result = super().analyze(data)
            return result.model_copy(update={"analyzer_version": "2.0.0"})

    case = open_case(client, AMBIGUOUS_ORDER)
    mid = case["message_id"]
    first = client.get(f"/messages/{mid}/analysis").json()

    with TestClient(create_app(settings=settings, engine=engine, analyzer=V2())) as c2:
        r = c2.post(f"/messages/{mid}/analyze")
        assert r.status_code == 201 and r.json()["analysis_created"]
        assert not r.json()["case_created"]
        assert r.json()["case"]["analysis_id"] == first["id"]  # the case keeps reviewing the ORIGINAL analysis

    history = client.get(f"/messages/{mid}/analyses").json()
    assert [a["analyzer_version"] for a in history] == [first["analyzer_version"], "2.0.0"]
    assert history[0] == first  # earlier analysis is byte-for-byte unchanged
    assert client.get(f"/messages/{mid}/analysis").json()["analyzer_version"] == "2.0.0"
    assert client.get(f"/cases/{case['id']}").json()["analysis"]["id"] == first["id"]


# --- cases ------------------------------------------------------------------------------------


def test_case_statuses_follow_the_analysis(client):
    clean = open_case(client, CLEAN_COMPLAINT)
    messy = open_case(client, AMBIGUOUS_ORDER)
    other = open_case(client, OTHER_TEXT)
    assert clean["status"] == "ready_for_review"
    assert messy["status"] == "needs_review" and other["status"] == "needs_review"


def test_import_status_analysis_and_review_status_are_separate(client):
    case = open_case(client)
    detail = client.get(f"/cases/{case['id']}").json()
    assert detail["message"]["import_status"] == "imported"
    assert detail["analysis"]["analysis_method"] == "rule_based"
    assert detail["analysis"]["analyzer_version"] and detail["case"]["status"] == "ready_for_review"
    assert detail["reviewer_label"] == "local_demo_user"


def test_case_detail_shows_original_email_fields_spans_and_problems(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    d = client.get(f"/cases/{case['id']}").json()
    assert "Pump Alpha" in d["message"]["body_text"]
    fields = {f["name"]: f for f in d["fields"]}
    qty = fields["quantity"]["values"][0]
    assert qty["origin"] == "analyzer" and qty["span"]["text"] == "10 pcs"
    assert [p["code"] for p in d["problems"]] == ["field_needs_review"]
    assert d["allowed_actions"] == ["edit", "approve_with_override", "reject"]


def test_case_list_filter_and_pagination(client):
    for text in (CLEAN_COMPLAINT, AMBIGUOUS_ORDER, OTHER_TEXT):
        open_case(client, text)
    assert client.get("/cases").json()["total"] == 3
    needs = client.get("/cases", params={"status": "needs_review"}).json()
    assert needs["total"] == 2 and all(c["status"] == "needs_review" for c in needs["items"])
    assert len(client.get("/cases", params={"limit": 1}).json()["items"]) == 1
    assert client.get("/cases", params={"status": "approved"}).json()["total"] == 0
    assert client.get("/cases", params={"status": "bogus"}).status_code == 422


def test_creating_a_case_is_audited(client):
    case = open_case(client)
    events = client.get(f"/cases/{case['id']}/audit").json()
    created = [e for e in events if e["event_type"] == "case_created"]
    assert len(created) == 1 and created[0]["actor"] == "system"
    assert created[0]["details"]["initial_status"] == case["status"]


# --- corrections --------------------------------------------------------------------------------


def edit(client, case_id, version, **body):
    return client.patch(f"/cases/{case_id}", json={"expected_version": version, **body})


def test_correcting_a_field_resolves_the_problem_and_triages_the_case(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    r = edit(client, case["id"], 1, corrections={"deadline": ["2026-12-15"]})
    assert r.status_code == 200
    d = r.json()
    assert d["case"]["version"] == 2 and d["case"]["status"] == "ready_for_review"
    deadline = next(f for f in d["fields"] if f["name"] == "deadline")
    assert deadline["corrected"] and deadline["values"][0]["origin"] == "human"
    assert deadline["analyzer_values"] == ["--12-15"]  # what the analyzer said stays visible
    assert d["problems"] == []


def test_a_human_edit_never_overwrites_the_original_email_or_analysis(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    mid = case["message_id"]
    message_before = client.get(f"/messages/{mid}").json()["message"]
    analysis_before = client.get(f"/messages/{mid}/analysis").json()
    edit(client, case["id"], 1, corrections={"deadline": ["2026-12-15"], "quantity": ["12 pcs"], "category": ["inquiry"]})
    assert client.get(f"/messages/{mid}").json()["message"] == message_before
    assert client.get(f"/messages/{mid}/analysis").json() == analysis_before


def test_corrections_history_is_preserved(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    edit(client, case["id"], 1, corrections={"quantity": ["12 pcs"]})
    edit(client, case["id"], 2, corrections={"quantity": ["15 pcs"]})
    edit(client, case["id"], 3, corrections={"quantity": None})  # revert to the analyzer's value
    h = client.get(f"/cases/{case['id']}/history").json()["corrections"]
    assert [(c["action"], c["values"], c["previous_values"]) for c in h] == [
        ("set", ["12 pcs"], ["10 pcs"]),
        ("set", ["15 pcs"], ["12 pcs"]),
        ("revert", [], ["15 pcs"]),
    ]
    assert [c["version_after"] for c in h] == [2, 3, 4]
    d = client.get(f"/cases/{case['id']}").json()
    qty = next(f for f in d["fields"] if f["name"] == "quantity")
    assert not qty["corrected"] and qty["values"][0]["value"] == "10 pcs"


def test_an_ambiguous_year_less_date_cannot_be_confirmed_as_is(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    d = client.get(f"/cases/{case['id']}").json()
    ambiguous = next(f for f in d["fields"] if f["name"] == "deadline")["values"][0]["value"]
    # a year-less date is not an acceptable human value, so an ambiguous date can not be
    # "confirmed" as it is: the human has to enter a real date
    bad = edit(client, case["id"], 1, corrections={"deadline": [ambiguous]})
    assert bad.status_code == 422 and error_code(bad) == "invalid_correction"


def test_confirming_the_analyzers_category_resolves_ambiguity(client):
    text = (
        "From: a@x.example\nSubject: Faktura FV/2026/0200 i reklamacja\nMessage-ID: <amb@x.example>\n\n"
        "Prosimy o przesłanie faktury FV/2026/0200. Mamy zastrzeżenia i składamy reklamację dostawy.\n"
    )
    case = open_case(client, text)
    d = client.get(f"/cases/{case['id']}").json()
    if "ambiguous_category" not in [p["code"] for p in d["problems"]]:
        pytest.skip("sample no longer ambiguous")
    r = edit(client, case["id"], 1, corrections={"category": [d["analyzer_category"]]})
    assert r.status_code == 200
    assert "ambiguous_category" not in [p["code"] for p in r.json()["problems"]]
    assert r.json()["category_corrected"]


def test_edit_with_a_review_note_only(client):
    case = open_case(client)
    r = edit(client, case["id"], 1, note="Spoke to the customer on the phone.")
    assert r.status_code == 200 and r.json()["case"]["version"] == 2
    h = client.get(f"/cases/{case['id']}/history").json()
    assert [(x["action"], x["note"]) for x in h["reviews"]] == [("note", "Spoke to the customer on the phone.")]
    assert h["corrections"] == []
    assert "case_note_added" in [e["event_type"] for e in client.get(f"/cases/{case['id']}/audit").json()]


@pytest.mark.parametrize(
    "corrections",
    [
        {"deadline": ["15 December"]},
        {"amount": ["12500"]},
        {"email": ["nope"]},
        {"category": ["approved"]},
        {"category": []},
        {"product": [" "]},
    ],
)
def test_invalid_corrections_are_422_and_change_nothing(client, corrections):
    case = open_case(client, AMBIGUOUS_ORDER)
    r = edit(client, case["id"], 1, corrections=corrections)
    assert r.status_code == 422
    assert client.get(f"/cases/{case['id']}").json()["case"]["version"] == 1
    assert client.get(f"/cases/{case['id']}/history").json()["corrections"] == []


def test_unknown_correction_target_is_422(client):
    case = open_case(client)
    r = edit(client, case["id"], 1, corrections={"status": ["approved"]})
    assert r.status_code == 422 and error_code(r) == "validation_error"


def test_empty_edit_is_422(client):
    case = open_case(client)
    assert edit(client, case["id"], 1).status_code == 422
    assert edit(client, case["id"], 1, corrections={}).status_code == 422


def test_a_correction_that_changes_nothing_is_refused_without_a_version_bump(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    edit(client, case["id"], 1, corrections={"quantity": ["12 pcs"]})
    r = edit(client, case["id"], 2, corrections={"quantity": ["12 pcs"]})
    assert r.status_code == 422
    assert client.get(f"/cases/{case['id']}").json()["case"]["version"] == 2


def test_removing_a_required_field_makes_the_case_need_review_again(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    edit(client, case["id"], 1, corrections={"deadline": ["2026-12-15"]})
    r = edit(client, case["id"], 2, corrections={"quantity": []})
    d = r.json()
    assert d["case"]["status"] == "needs_review"
    assert [p["code"] for p in d["problems"]] == ["missing_required_field"]
    types = [e["event_type"] for e in client.get(f"/cases/{case['id']}/audit").json()]
    assert types.count("status_changed") == 2  # needs->ready, then ready->needs


def test_edit_missing_case_is_404(client):
    r = edit(client, 777, 1, note="x")
    assert r.status_code == 404 and error_code(r) == "case_not_found"


# --- review decisions -----------------------------------------------------------------------------


def post(client, case_id, action, version, **body):
    return client.post(f"/cases/{case_id}/{action}", json={"expected_version": version, **body})


def test_approve_a_clean_case(client):
    case = open_case(client)
    r = post(client, case["id"], "approve", 1, note="Checked against the ERP.")
    assert r.status_code == 200
    d = r.json()
    assert d["case"]["status"] == "approved" and d["case"]["decided_at"] and d["case"]["version"] == 2
    assert d["allowed_actions"] == ["reopen"]


def test_nothing_is_approved_without_an_explicit_call(client):
    for text in (CLEAN_COMPLAINT, AMBIGUOUS_ORDER, OTHER_TEXT, INJECTION_TEXT):
        open_case(client, text)
    for name in ("01_invoice_dispute_pl.eml", "03_contract_renewal_en.eml", "09_payment_reminder_en.txt"):
        data = (SAMPLES / name).read_bytes()
        if name.endswith(".txt"):
            client.post("/messages/text", json={"text": data.decode("utf-8")})
        else:
            client.post("/messages/eml", content=data, headers=EML)
    for m in client.get("/messages").json()["items"]:
        if m["import_status"] == "imported":
            client.post(f"/messages/{m['id']}/analyze")
    statuses = {c["status"] for c in client.get("/cases").json()["items"]}
    assert statuses <= {"needs_review", "ready_for_review"}
    assert client.get("/cases", params={"status": "approved"}).json()["total"] == 0


def test_approval_is_blocked_while_the_data_has_problems(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    r = post(client, case["id"], "approve", 1)
    assert r.status_code == 422 and error_code(r) == "approval_blocked"
    problems = r.json()["error"]["details"]["problems"]
    assert [p["code"] for p in problems] == ["field_needs_review"]
    d = client.get(f"/cases/{case['id']}").json()
    assert d["case"]["status"] == "needs_review" and d["case"]["version"] == 1  # nothing changed


def test_approval_validates_the_data_after_human_corrections(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    edit(client, case["id"], 1, corrections={"deadline": ["2026-12-15"]})
    r = post(client, case["id"], "approve", 2)
    assert r.status_code == 200 and r.json()["case"]["status"] == "approved"


def test_a_correction_can_also_make_a_clean_case_unapprovable(client):
    case = open_case(client)  # clean complaint
    edit(client, case["id"], 1, corrections={"reference": []})
    r = post(client, case["id"], "approve", 2)
    assert r.status_code == 422 and error_code(r) == "approval_blocked"


def test_override_needs_a_written_reason_and_is_recorded_everywhere(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    short = post(client, case["id"], "approve", 1, override_reason="ok")
    assert short.status_code == 422 and error_code(short) == "validation_error"
    r = post(client, case["id"], "approve", 1, override_reason="Customer confirmed the date by phone.")
    assert r.status_code == 200 and r.json()["case"]["status"] == "approved"

    review = client.get(f"/cases/{case['id']}/history").json()["reviews"][-1]
    assert review["action"] == "approve"
    assert review["override_reason"] == "Customer confirmed the date by phone."
    assert [p["code"] for p in review["overridden_problems"]] == ["field_needs_review"]
    event = next(e for e in client.get(f"/cases/{case['id']}/audit").json() if e["event_type"] == "case_approved")
    assert event["details"]["override_used"] is True
    assert event["details"]["overridden_problem_codes"] == ["field_needs_review"]


def test_an_override_reason_on_a_clean_case_is_not_recorded_as_an_override(client):
    case = open_case(client)
    post(client, case["id"], "approve", 1, override_reason="not actually needed here")
    event = next(e for e in client.get(f"/cases/{case['id']}/audit").json() if e["event_type"] == "case_approved")
    assert event["details"]["override_used"] is False and event["details"]["override_reason"] is None


def test_prompt_injection_email_needs_an_override_to_be_approved(client):
    case = open_case(client, INJECTION_TEXT)
    assert case["status"] == "needs_review"
    r = post(client, case["id"], "approve", 1)
    assert r.status_code == 422
    assert "possible_prompt_injection" in [p["code"] for p in r.json()["error"]["details"]["problems"]]
    fixed = edit(client, case["id"], 1, corrections={"category": ["document_request"]})
    assert "possible_prompt_injection" in [p["code"] for p in fixed.json()["problems"]]


def test_reject_needs_a_reason_and_works(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    assert client.post(f"/cases/{case['id']}/reject", json={"expected_version": 1}).status_code == 422
    r = post(client, case["id"], "reject", 1, reason="Duplicate of an order we already have.")
    assert r.status_code == 200 and r.json()["case"]["status"] == "rejected"
    events = [e["event_type"] for e in client.get(f"/cases/{case['id']}/audit").json()]
    assert events[-1] == "case_rejected"


def test_rejecting_does_not_require_valid_data(client):
    case = open_case(client, OTHER_TEXT)
    assert post(client, case["id"], "reject", 1, reason="spam").status_code == 200


def test_invalid_transitions_are_409(client):
    case = open_case(client)
    cid = case["id"]
    post(client, cid, "approve", 1)  # -> approved, v2
    for action, body in (("approve", {}), ("reject", {"reason": "nope"})):
        r = post(client, cid, action, 2, **body)
        assert r.status_code == 409 and error_code(r) == "invalid_transition"
    r = edit(client, cid, 2, note="late note")
    assert r.status_code == 409 and error_code(r) == "case_not_editable"
    d = client.get(f"/cases/{cid}").json()
    assert d["case"]["status"] == "approved" and d["case"]["version"] == 2


def test_a_rejected_case_can_not_be_approved_directly(client):
    case = open_case(client)
    post(client, case["id"], "reject", 1, reason="wrong customer")
    r = post(client, case["id"], "approve", 2)
    assert r.status_code == 409 and error_code(r) == "invalid_transition"


def test_reopen_only_works_on_decided_cases(client):
    case = open_case(client)
    r = post(client, case["id"], "reopen", 1, reason="why not")
    assert r.status_code == 409 and error_code(r) == "invalid_transition"


def test_reopen_edit_and_approve_again(client):
    case = open_case(client)
    cid = case["id"]
    post(client, cid, "approve", 1)
    r = post(client, cid, "reopen", 2, reason="Customer sent a corrected invoice number.")
    assert r.status_code == 200 and r.json()["case"]["status"] == "ready_for_review"
    assert r.json()["case"]["decided_at"] is None
    assert edit(client, cid, 3, corrections={"reference": ["FV/2026/0043"]}).status_code == 200
    r = post(client, cid, "approve", 4)
    assert r.status_code == 200 and r.json()["case"]["status"] == "approved" and r.json()["case"]["version"] == 5
    reviews = client.get(f"/cases/{cid}/history").json()["reviews"]
    assert [x["action"] for x in reviews] == ["approve", "reopen", "approve"]
    # the first decision is preserved, with the data that was approved at that time
    assert reviews[0]["snapshot"]["fields"]["reference"] == ["FV/2026/0042"]
    assert reviews[2]["snapshot"]["fields"]["reference"] == ["FV/2026/0043"]


def test_reopening_a_problematic_case_returns_it_to_needs_review(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    post(client, case["id"], "reject", 1, reason="first thought it was spam")
    r = post(client, case["id"], "reopen", 2, reason="it was a real order")
    assert r.json()["case"]["status"] == "needs_review"


# --- optimistic locking ----------------------------------------------------------------------------


def test_stale_edit_is_refused_and_the_newer_edit_survives(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    cid = case["id"]
    assert edit(client, cid, 1, corrections={"quantity": ["12 pcs"]}).status_code == 200
    stale = edit(client, cid, 1, corrections={"quantity": ["99 pcs"]})  # based on the old version
    assert stale.status_code == 409 and error_code(stale) == "version_conflict"
    assert stale.json()["error"]["details"]["current_version"] == 2
    d = client.get(f"/cases/{cid}").json()
    qty = next(f for f in d["fields"] if f["name"] == "quantity")
    assert qty["values"][0]["value"] == "12 pcs"


def test_stale_approval_is_refused(client):
    case = open_case(client)
    edit(client, case["id"], 1, note="a colleague added a note")
    r = post(client, case["id"], "approve", 1)
    assert r.status_code == 409 and error_code(r) == "version_conflict"
    assert client.get(f"/cases/{case['id']}").json()["case"]["status"] != "approved"


def test_a_future_version_is_also_refused(client):
    case = open_case(client)
    assert post(client, case["id"], "approve", 7).status_code == 409


def test_double_submit_of_the_same_approval_is_safe(client):
    case = open_case(client)
    first = post(client, case["id"], "approve", 1)
    second = post(client, case["id"], "approve", 1)  # the browser retried
    assert first.status_code == 200 and second.status_code == 409
    approvals = [e for e in client.get(f"/cases/{case['id']}/audit").json() if e["event_type"] == "case_approved"]
    assert len(approvals) == 1


@pytest.mark.parametrize("version", [0, -3, "x", None, 1.5])
def test_expected_version_is_validated(client, version):
    case = open_case(client)
    r = client.post(f"/cases/{case['id']}/approve", json={"expected_version": version})
    assert r.status_code == 422


def test_expected_version_is_required(client):
    case = open_case(client)
    assert client.post(f"/cases/{case['id']}/approve", json={}).status_code == 422
    assert client.patch(f"/cases/{case['id']}", json={"note": "x"}).status_code == 422


# --- audit ------------------------------------------------------------------------------------------


def test_audit_trail_of_a_complete_workflow(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    cid = case["id"]
    edit(client, cid, 1, corrections={"deadline": ["2026-12-15"]}, note="Date confirmed by phone.")
    post(client, cid, "approve", 2)
    post(client, cid, "reopen", 3, reason="Customer changed the order.")
    post(client, cid, "reject", 4, reason="Order cancelled.")

    events = client.get(f"/cases/{cid}/audit").json()
    assert [e["event_type"] for e in events] == [
        "message_imported", "analysis_completed", "case_created",
        "case_updated", "status_changed", "case_note_added",
        "case_approved", "case_reopened", "case_rejected",
    ]
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
    assert events == client.get(f"/messages/{case['message_id']}/audit").json()


def test_every_mutation_leaves_an_audit_event_with_its_context(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    cid = case["id"]
    edit(client, cid, 1, corrections={"quantity": ["12 pcs"]})
    events = client.get(f"/cases/{cid}/audit").json()
    updated = next(e for e in events if e["event_type"] == "case_updated")
    d = updated["details"]
    assert d["case_id"] == cid and d["reviewer"] == "local_demo_user"
    assert d["version_before"] == 1 and d["version_after"] == 2
    assert d["changes"] == [{"target": "quantity", "action": "set", "before": ["10 pcs"], "after": ["12 pcs"]}]


def test_approval_audit_records_exactly_what_was_approved(client):
    case = open_case(client)
    post(client, case["id"], "approve", 1, note="fine")
    ev = next(e for e in client.get(f"/cases/{case['id']}/audit").json() if e["event_type"] == "case_approved")
    assert ev["actor"] == "human" and ev["details"]["reviewer"] == "local_demo_user"
    assert ev["details"]["approved_data"]["category"] == "complaint"
    assert ev["details"]["approved_data"]["fields"]["reference"] == ["FV/2026/0042"]
    assert ev["details"]["from"] == "ready_for_review" and ev["details"]["to"] == "approved"


def test_decisions_are_never_attributed_to_the_system(client):
    case = open_case(client)
    cid = case["id"]
    post(client, cid, "approve", 1)
    for e in client.get(f"/cases/{cid}/audit").json():
        if e["event_type"] in {"case_approved", "case_rejected", "case_reopened", "case_updated", "case_note_added"}:
            assert e["actor"] == "human"
        if e["event_type"] in {"case_created", "status_changed", "analysis_completed"}:
            assert e["actor"] == "system"


def test_failed_requests_leave_no_audit_trace_and_no_partial_state(client):
    case = open_case(client, AMBIGUOUS_ORDER)
    before = client.get(f"/cases/{case['id']}/audit").json()
    post(client, case["id"], "approve", 1)  # blocked
    edit(client, case["id"], 1, corrections={"deadline": ["bad"]})  # invalid
    edit(client, case["id"], 9, note="x")  # stale
    assert client.get(f"/cases/{case['id']}/audit").json() == before
    assert client.get(f"/cases/{case['id']}/history").json() == {"case_id": case["id"], "corrections": [], "reviews": []}


# --- security -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["actor", "reviewer", "user", "status", "approved", "version"])
def test_client_supplied_identity_or_status_is_rejected(client, field):
    case = open_case(client)
    for path, body in (
        (f"/cases/{case['id']}/approve", {"expected_version": 1}),
        (f"/cases/{case['id']}/reject", {"expected_version": 1, "reason": "no"}),
        (f"/cases/{case['id']}/reopen", {"expected_version": 1, "reason": "no"}),
    ):
        r = client.post(path, json={**body, field: "admin"})
        assert r.status_code == 422 and error_code(r) == "validation_error"
    r = client.patch(f"/cases/{case['id']}", json={"expected_version": 1, "note": "x", field: "admin"})
    assert r.status_code == 422
    assert client.get(f"/cases/{case['id']}").json()["case"]["status"] == "ready_for_review"


def test_the_actor_header_is_ignored(client):
    case = open_case(client)
    client.post(f"/cases/{case['id']}/approve", json={"expected_version": 1},
                headers={"X-Actor": "ceo", "X-User": "ceo", "Authorization": "Bearer abc"})
    ev = next(e for e in client.get(f"/cases/{case['id']}/audit").json() if e["event_type"] == "case_approved")
    assert ev["details"]["reviewer"] == "local_demo_user" and "ceo" not in json.dumps(ev)


def test_unexpected_host_headers_are_refused(client):
    r = client.get("/health", headers={"Host": "evil.example"})
    assert r.status_code == 400
    assert client.get("/health", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.get("/health", headers={"Host": "127.0.0.1:8000"}).status_code == 200


def test_no_cors_headers_are_ever_granted(client):
    r = client.options(
        "/messages/eml",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in r.headers
    r = client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_sql_looking_input_is_just_text(client):
    evil = "x'; DROP TABLE cases; --"
    mid = import_text(client, f"From: a@x.example\nSubject: {evil}\nMessage-ID: <sql@x.example>\n\n{evil}")
    client.post(f"/messages/{mid}/analyze")
    assert client.get("/cases").json()["total"] == 1
    case_id = client.get("/cases").json()["items"][0]["id"]
    assert edit(client, case_id, 1, corrections={"product": [evil]}).status_code == 200
    assert client.get("/cases").json()["total"] == 1


def test_html_in_email_is_returned_as_data_not_interpreted(client):
    mid = import_text(client, "From: a@x.example\nSubject: <script>alert(1)</script>\nMessage-ID: <h@x.example>\n\nhi")
    r = client.get(f"/messages/{mid}")
    assert r.headers["content-type"].startswith("application/json")
    assert "<script>" in r.json()["message"]["subject"]


def test_internal_errors_do_not_leak_details(engine, settings):
    from fastapi.testclient import TestClient

    from ai_ops_hub.analysis import RuleBasedAnalyzer
    from ai_ops_hub.api import create_app

    class Broken(RuleBasedAnalyzer):
        def analyze(self, data):
            raise RuntimeError("secret-internal-path C:\\Users\\x\\key.pem")

    with TestClient(create_app(settings=settings, engine=engine, analyzer=Broken()), raise_server_exceptions=False) as c:
        mid = import_text(c, CLEAN_COMPLAINT)
        r = c.post(f"/messages/{mid}/analyze")
        assert r.status_code == 500 and error_code(r) == "internal_error"
        assert "secret" not in r.text and "key.pem" not in r.text
        assert c.get("/cases").json()["total"] == 0  # nothing half-created


def test_a_lying_analyzer_is_rejected_and_creates_no_case(engine, settings):
    from fastapi.testclient import TestClient

    from ai_ops_hub.analysis import RuleBasedAnalyzer
    from ai_ops_hub.api import create_app
    from ai_ops_hub.schemas import AnalysisMethod

    class Liar(RuleBasedAnalyzer):
        def analyze(self, data):
            result = super().analyze(data)
            return result.model_copy(update={"analysis_method": AnalysisMethod.LLM})

    with TestClient(create_app(settings=settings, engine=engine, analyzer=Liar())) as c:
        mid = import_text(c, CLEAN_COMPLAINT)
        r = c.post(f"/messages/{mid}/analyze")
        assert r.status_code == 500 and error_code(r) == "analyzer_contract_violation"
        assert c.get("/cases").json()["total"] == 0


# --- persistence, atomicity, concurrency ------------------------------------------------------------


def test_everything_survives_an_application_restart(file_db_url, settings):
    from fastapi.testclient import TestClient

    from ai_ops_hub.api import create_app
    from ai_ops_hub.db.engine import create_db_engine

    eng1 = create_db_engine(file_db_url, settings)
    with TestClient(create_app(settings=settings, engine=eng1)) as c1:
        case = open_case(c1, AMBIGUOUS_ORDER)
        cid = case["id"]
        edit(c1, cid, 1, corrections={"deadline": ["2026-12-15"]}, note="confirmed")
        post(c1, cid, "approve", 2, note="ok")
        before = {
            "case": c1.get(f"/cases/{cid}").json(),
            "history": c1.get(f"/cases/{cid}/history").json(),
            "audit": c1.get(f"/cases/{cid}/audit").json(),
            "analysis": c1.get(f"/messages/{case['message_id']}/analysis").json(),
        }
    eng1.dispose()

    eng2 = create_db_engine(file_db_url, settings)  # a brand-new process would do exactly this
    try:
        with TestClient(create_app(settings=settings, engine=eng2)) as c2:
            assert c2.get(f"/cases/{cid}").json() == before["case"]
            assert c2.get(f"/cases/{cid}/history").json() == before["history"]
            assert c2.get(f"/cases/{cid}/audit").json() == before["audit"]
            assert c2.get(f"/messages/{case['message_id']}/analysis").json() == before["analysis"]
            assert c2.get(f"/cases/{cid}").json()["case"]["status"] == "approved"
            # and the workflow continues where it stopped
            assert post(c2, cid, "reopen", 3, reason="after restart").status_code == 200
            assert c2.post(f"/messages/{case['message_id']}/analyze").status_code == 200  # idempotent
    finally:
        eng2.dispose()


def test_a_failure_while_writing_the_audit_event_rolls_the_decision_back(client, monkeypatch):
    from ai_ops_hub.db.repositories import AuditRepository

    case = open_case(client)
    original = AuditRepository.add

    def boom(self, **kw):
        if kw["event_type"].value == "case_approved":
            raise RuntimeError("disk full")
        return original(self, **kw)

    monkeypatch.setattr(AuditRepository, "add", boom)
    from fastapi.testclient import TestClient

    with TestClient(client.app, raise_server_exceptions=False) as c:
        r = post(c, case["id"], "approve", 1)
        assert r.status_code == 500
    monkeypatch.undo()
    d = client.get(f"/cases/{case['id']}").json()
    assert d["case"]["status"] == "ready_for_review" and d["case"]["version"] == 1
    assert client.get(f"/cases/{case['id']}/history").json()["reviews"] == []  # decision record rolled back too
    assert "case_approved" not in [e["event_type"] for e in client.get(f"/cases/{case['id']}/audit").json()]


def test_concurrent_approvals_produce_exactly_one_decision(file_db_url, settings):
    import threading

    from ai_ops_hub.api import create_app
    from ai_ops_hub.db.engine import create_db_engine
    from ai_ops_hub.errors import AIOpsError

    eng = create_db_engine(file_db_url, settings)
    try:
        app = create_app(settings=settings, engine=eng)
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            case = open_case(c, AMBIGUOUS_ORDER)
            edit(c, case["id"], 1, corrections={"deadline": ["2026-12-15"]})
        cases = app.state.services.cases
        outcomes: list[str] = []
        barrier = threading.Barrier(6)

        def worker(i: int) -> None:
            barrier.wait()
            try:
                cases.approve(case["id"], 2, note=f"worker {i}")
                outcomes.append("ok")
            except AIOpsError as exc:
                outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert outcomes.count("ok") == 1
        assert set(outcomes) - {"ok"} <= {"VersionConflictError", "InvalidTransitionError"}
        detail = cases.get_case(case["id"])
        assert detail.case.status.value == "approved" and detail.case.version == 3
        audit = cases.audit(case["id"])
        assert [e.event_type.value for e in audit].count("case_approved") == 1
        assert [r.action.value for r in cases.history(case["id"]).reviews] == ["approve"]
    finally:
        eng.dispose()


def test_concurrent_edits_never_lose_an_update(file_db_url, settings):
    import threading

    from ai_ops_hub.api import create_app
    from ai_ops_hub.db.engine import create_db_engine
    from ai_ops_hub.errors import VersionConflictError

    eng = create_db_engine(file_db_url, settings)
    try:
        app = create_app(settings=settings, engine=eng)
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            case = open_case(c, AMBIGUOUS_ORDER)
        cases = app.state.services.cases
        results: list[tuple[int, str]] = []
        barrier = threading.Barrier(5)

        def worker(i: int) -> None:
            barrier.wait()
            try:
                cases.edit_case(case["id"], 1, {"quantity": [f"{i + 1} pcs"]})
                results.append((i, "ok"))
            except VersionConflictError:
                results.append((i, "stale"))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        winners = [i for i, r in results if r == "ok"]
        assert len(winners) == 1
        detail = cases.get_case(case["id"])
        qty = next(f for f in detail.fields if f.name.value == "quantity")
        assert qty.values[0].value == f"{winners[0] + 1} pcs"  # the single winner's value, intact
        assert detail.case.version == 2
        assert len(cases.history(case["id"]).corrections) == 1
    finally:
        eng.dispose()


def test_reads_work_while_a_write_is_in_progress(file_db_url, settings):
    """A reader is not blocked by an open write transaction (WAL, deferred reads)."""
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from ai_ops_hub.api import create_app
    from ai_ops_hub.db.engine import create_db_engine, for_writes

    eng = create_db_engine(file_db_url, settings)
    try:
        with TestClient(create_app(settings=settings, engine=eng)) as c:
            case = open_case(c)
            with for_writes(eng).begin() as writer:  # holds the write lock
                writer.execute(text("UPDATE cases SET updated_at = updated_at"))
                assert c.get(f"/cases/{case['id']}").status_code == 200
                assert c.get("/cases").status_code == 200
    finally:
        eng.dispose()
