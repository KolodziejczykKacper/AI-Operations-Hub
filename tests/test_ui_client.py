"""The UI's HTTP client: error mapping, timeouts, safety defaults. No network, no Streamlit."""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest
from conftest import AMBIGUOUS_ORDER, CLEAN_COMPLAINT, EML, SAMPLES

from ai_ops_hub.ui import client as C
from ai_ops_hub.ui.client import ApiClient

NOW = "2026-11-17T10:00:00Z"


def make(handler, **kw) -> tuple[ApiClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(wrapped))
    return ApiClient("http://127.0.0.1:8000", http=http, **kw), seen


def err(status: int, code: str, message: str = "boom", details=None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message, "details": details}})


SUMMARY = {
    "id": 1, "message_id": 1, "analysis_id": 1, "status": "needs_review", "version": 1,
    "created_at": NOW, "updated_at": NOW, "decided_at": None, "subject": "S",
    "effective_category": "order", "imported_at": NOW, "future_field": "ignored",
}


# --- construction and safety defaults ---------------------------------------------------------------


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:9000", "http://[::1]:8000/", "http://127.0.0.2"])
def test_loopback_addresses_are_accepted(url):
    assert ApiClient(url).base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url", ["http://example.com", "https://api.example.org:8000", "http://192.168.1.10:8000", "http://0.0.0.0:8000",
            "ftp://127.0.0.1", "127.0.0.1:8000", "", "http://"],
)
def test_non_local_or_malformed_addresses_are_refused(url):
    with pytest.raises(ValueError):
        ApiClient(url)


def test_a_remote_address_needs_an_explicit_opt_in():
    assert ApiClient("http://example.com", allow_remote=True).base_url == "http://example.com"


def test_default_base_url_and_timeouts():
    assert C.DEFAULT_BASE_URL == "http://127.0.0.1:8000"
    t = C.DEFAULT_TIMEOUT
    assert t.connect and t.read and t.write and t.pool  # every phase has a limit
    client = ApiClient()
    assert client._http.timeout == t
    assert ApiClient(timeout=2.5)._http.timeout.read == 2.5


def test_the_http_client_ignores_proxies_and_does_not_follow_redirects():
    client = ApiClient()
    assert client._http.follow_redirects is False
    assert client._http.trust_env is False


def test_a_redirect_is_an_error_not_a_silent_hop_to_another_host():
    def handler(request):
        return httpx.Response(307, headers={"location": "http://evil.example/steal"}, json={"x": 1})

    client, seen = make(handler)
    client._http.follow_redirects = False
    with pytest.raises(C.ApiError):
        client.health()
    assert len(seen) == 1 and all(r.url.host == "127.0.0.1" for r in seen)


# --- requests are what the API expects ------------------------------------------------------------------


def test_every_request_goes_to_the_configured_host_only():
    def handler(request):
        return httpx.Response(200, json={"status": "ok", "database": "ok"})

    client, seen = make(handler)
    client.health()
    assert {r.url.host for r in seen} == {"127.0.0.1"}


def test_list_cases_sends_filters_and_drops_empty_ones():
    client, seen = make(lambda r: httpx.Response(200, json={"items": [SUMMARY], "total": 1, "limit": 5, "offset": 10}))
    page = client.list_cases(status="needs_review", q=None, limit=5, offset=10)
    assert dict(seen[0].url.params) == {"status": "needs_review", "limit": "5", "offset": "10"}
    assert page.total == 1 and page.items[0].subject == "S"
    client.list_cases(q="alpha")
    assert dict(seen[1].url.params)["q"] == "alpha" and "status" not in seen[1].url.params


def test_edit_sends_the_expected_version_and_corrections():
    detail = {"case": SUMMARY}
    client, seen = make(lambda r: err(409, "version_conflict", "stale", {"current_version": 4}))
    with pytest.raises(C.VersionConflict):
        client.edit_case(1, 3, {"deadline": ["2026-12-15"], "product": None}, "note")
    body = json.loads(seen[0].content)
    assert seen[0].method == "PATCH" and seen[0].url.path == "/cases/1"
    assert body == {"expected_version": 3, "corrections": {"deadline": ["2026-12-15"], "product": None}, "note": "note"}
    assert detail


def test_decisions_send_version_and_reason():
    client, seen = make(lambda r: err(409, "invalid_transition", "no"))
    for fn, path in ((client.reject, "reject"), (client.reopen, "reopen")):
        with pytest.raises(C.ApiError):
            fn(7, 2, "because")
        assert seen[-1].url.path == f"/cases/7/{path}"
        assert json.loads(seen[-1].content) == {"expected_version": 2, "reason": "because"}
    with pytest.raises(C.ApiError):
        client.approve(7, 2, note="n", override_reason="a long enough reason")
    assert json.loads(seen[-1].content) == {
        "expected_version": 2, "note": "n", "override_reason": "a long enough reason",
    }


def test_approve_without_extras_sends_only_the_version():
    client, seen = make(lambda r: err(409, "invalid_transition", "no"))
    with pytest.raises(C.ApiError):
        client.approve(1, 5)
    assert json.loads(seen[0].content) == {"expected_version": 5}


def test_eml_is_posted_as_raw_bytes_with_the_right_content_type():
    data = (SAMPLES / "01_invoice_dispute_pl.eml").read_bytes()
    client, seen = make(lambda r: err(422, "malformed_message", "bad"))
    with pytest.raises(C.ApiError):
        client.import_eml(data)
    assert seen[0].content == data and seen[0].headers["content-type"] == "message/rfc822"


# --- error mapping -----------------------------------------------------------------------------------------


def test_version_conflict_carries_the_current_version():
    client, _ = make(lambda r: err(409, "version_conflict", "stale", {"current_version": 9}))
    with pytest.raises(C.VersionConflict) as exc:
        client.approve(1, 1)
    assert exc.value.current_version == 9 and exc.value.status == 409 and exc.value.code == "version_conflict"


def test_approval_blocked_carries_the_problems():
    problems = [{"code": "missing_required_field", "message": "no product", "field": "product"}]
    client, _ = make(lambda r: err(422, "approval_blocked", "blocked", {"problems": problems}))
    with pytest.raises(C.ApprovalBlocked) as exc:
        client.approve(1, 1)
    assert exc.value.problems == problems


def test_invalid_correction_carries_the_problems():
    client, _ = make(lambda r: err(422, "invalid_correction", "bad", {"problems": [{"value": "x", "message": "no"}]}))
    with pytest.raises(C.InvalidCorrection) as exc:
        client.edit_case(1, 1, {"deadline": ["x"]})
    assert exc.value.problems[0]["value"] == "x"


def test_validation_error_is_its_own_type():
    client, _ = make(lambda r: err(422, "validation_error", "invalid", {"problems": []}))
    with pytest.raises(C.ValidationFailed):
        client.edit_case(1, 1, {})


@pytest.mark.parametrize(("status", "code"), [(404, "case_not_found"), (409, "invalid_transition"), (413, "payload_too_large"), (500, "internal_error")])
def test_other_envelope_errors_become_api_errors(status, code):
    client, _ = make(lambda r: err(status, code, "msg"))
    with pytest.raises(C.ApiError) as exc:
        client.get_case(1)
    assert (exc.value.status, exc.value.code, str(exc.value)) == (status, code, "msg")
    assert type(exc.value) is C.ApiError


def test_error_with_a_garbled_envelope_still_gives_a_safe_error():
    for body in ({"detail": "x"}, {"error": "just text"}, ["a"], {"error": {}}):
        client, _ = make(lambda r, b=body: httpx.Response(500, json=b))
        with pytest.raises(C.ApiError) as exc:
            client.get_case(1)
        assert exc.value.status == 500


def test_malformed_malformed_details_do_not_crash_the_mapping():
    client, _ = make(lambda r: err(409, "version_conflict", "x", "not-a-dict"))
    with pytest.raises(C.VersionConflict) as exc:
        client.approve(1, 1)
    assert exc.value.current_version is None
    client, _ = make(lambda r: err(422, "approval_blocked", "x", {"problems": "nope"}))
    with pytest.raises(C.ApprovalBlocked) as exc:
        client.approve(1, 1)
    assert exc.value.problems == []


def test_non_json_answers_are_an_invalid_response():
    client, _ = make(lambda r: httpx.Response(200, text="<html>proxy error</html>"))
    with pytest.raises(C.InvalidResponse):
        client.health()
    client, _ = make(lambda r: httpx.Response(502, text="Bad gateway"))
    with pytest.raises(C.InvalidResponse):
        client.stats()


@pytest.mark.parametrize("payload", [{"total": "many", "by_status": {}}, {"total": 1}, [], None, "x"])
def test_answers_that_do_not_match_the_contract_are_rejected(payload):
    client, _ = make(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(C.InvalidResponse):
        client.stats()


def test_unknown_response_fields_are_ignored():
    client, _ = make(lambda r: httpx.Response(200, json={"items": [SUMMARY], "total": 1, "limit": 1, "offset": 0, "new": 1}))
    assert client.list_cases().items[0].id == 1


def test_audit_must_be_a_list():
    client, _ = make(lambda r: httpx.Response(200, json={"not": "a list"}))
    with pytest.raises(C.InvalidResponse):
        client.case_audit(1)


# --- backend unavailable ---------------------------------------------------------------------------------------


def test_connection_refused_is_backend_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    client, _ = make(handler)
    with pytest.raises(C.BackendUnavailable) as exc:
        client.stats()
    assert "Is the backend running" in str(exc.value) and not exc.value.outcome_unknown
    assert "refused" not in str(exc.value)  # low-level detail is not shown to the user


def test_connection_refused_on_a_real_closed_port():
    client = ApiClient("http://127.0.0.1:1", timeout=1.0)  # nothing listens there
    with pytest.raises(C.BackendUnavailable):
        client.health()


def test_timeout_on_a_read_is_just_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    client, _ = make(handler)
    with pytest.raises(C.BackendUnavailable) as exc:
        client.stats()
    assert not exc.value.outcome_unknown and "in time" in str(exc.value)


def test_timeout_on_a_write_says_the_outcome_is_unknown():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    client, _ = make(handler)
    with pytest.raises(C.BackendUnavailable) as exc:
        client.approve(1, 1)
    assert exc.value.outcome_unknown


def test_other_transport_errors_are_wrapped():
    def handler(request):
        raise httpx.RemoteProtocolError("connection dropped")

    client, _ = make(handler)
    with pytest.raises(C.BackendUnavailable) as exc:
        client.reject(1, 1, "x")
    assert exc.value.outcome_unknown


def test_writes_are_never_retried():
    calls = []

    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("slow")

    client, _ = make(handler)
    for fn in (lambda: client.approve(1, 1), lambda: client.reject(1, 1, "reason"), lambda: client.import_text("x"),
               lambda: client.edit_case(1, 1, {}, "n"), lambda: client.reopen(1, 1, "reason"), lambda: client.analyze(1)):
        with pytest.raises(C.BackendUnavailable):
            fn()
    assert len(calls) == 6  # exactly one attempt each


def test_ids_are_forced_to_integers_in_paths():
    client, seen = make(lambda r: err(404, "case_not_found", "no"))
    with pytest.raises((C.ApiError, ValueError, TypeError)):
        client.get_case("1/../../admin")
    assert not seen


# --- against the real API (in process) ------------------------------------------------------------------------


@pytest.fixture()
def live(client):
    return ApiClient("http://127.0.0.1:8000", http=client)


def test_full_round_trip_through_the_real_api(live):
    r = live.import_text(CLEAN_COMPLAINT)
    assert r.decision == "new" and r.message.import_status == "imported"
    a = live.analyze(r.message.id)
    assert a.analysis_created and a.analysis.analysis_method == "rule_based"
    detail = live.get_case(a.case.id)
    assert detail.effective_category == "complaint" and detail.allowed_actions == ["edit", "approve", "reject"]
    assert live.stats().by_status["ready_for_review"] == 1
    approved = live.approve(detail.case.id, detail.case.version, note="ok")
    assert approved.case.status == "approved" and approved.allowed_actions == ["reopen"]
    assert [e.event_type for e in live.case_audit(detail.case.id)][-1] == "case_approved"
    assert live.history(detail.case.id).reviews[0].action == "approve"
    with pytest.raises(C.VersionConflict) as exc:
        live.approve(detail.case.id, detail.case.version)
    assert exc.value.current_version == approved.case.version


def test_import_duplicate_and_conflict_are_results_not_errors(live):
    first = live.import_eml((SAMPLES / "01_invoice_dispute_pl.eml").read_bytes())
    again = live.import_eml((SAMPLES / "01_invoice_dispute_pl.eml").read_bytes())
    conflict = live.import_eml((SAMPLES / "02_invoice_dispute_pl_conflict.eml").read_bytes())
    assert (first.decision, again.decision, conflict.decision) == ("new", "duplicate", "conflict")
    assert conflict.message.import_status == "conflict" and conflict.existing_message.id == first.message.id
    with pytest.raises(C.ApiError) as exc:
        live.analyze(conflict.message.id)
    assert exc.value.code == "conflict_not_analyzable"


def test_server_validation_errors_reach_the_caller(live):
    r = live.import_text(AMBIGUOUS_ORDER)
    case = live.analyze(r.message.id).case
    with pytest.raises(C.InvalidCorrection) as exc:
        live.edit_case(case.id, case.version, {"deadline": ["15 December"]})
    assert exc.value.problems
    with pytest.raises(C.ApprovalBlocked) as blocked:
        live.approve(case.id, case.version)
    assert blocked.value.problems


def test_not_found_and_empty_import_map_to_api_errors(live):
    with pytest.raises(C.ApiError) as exc:
        live.get_case(999)
    assert exc.value.code == "case_not_found" and exc.value.status == 404
    with pytest.raises(C.ApiError) as exc:
        live.import_eml(b"")
    assert exc.value.code == "empty_input"


def test_eml_content_type_constant_matches_the_api(client):
    assert client.post("/messages/eml", content=b"x", headers=EML).status_code in (201, 422)
