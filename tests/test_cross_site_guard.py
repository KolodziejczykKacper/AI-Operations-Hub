"""State-changing browser requests from another web origin are refused; non-browser clients are unaffected."""

from __future__ import annotations

import pytest
from conftest import CLEAN_COMPLAINT, import_text


def err(r):
    return r.json()["error"]["code"]


@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
@pytest.mark.parametrize("method, path, kw", [
    ("post", "/messages/text", {"json": {"text": "Subject: x\n\nhello"}}),
    ("post", "/messages/1/analyze", {}),
    ("post", "/cases/1/approve", {"json": {"expected_version": 1}}),
    ("post", "/messages/1/claude/analyze", {"json": {}}),
    ("patch", "/cases/1", {"json": {"expected_version": 1, "note": "x"}}),
    ("delete", "/cases/1", {}),
])
def test_a_hostile_web_page_cannot_change_anything(client, site, method, path, kw):
    r = getattr(client, method)(path, headers={"Sec-Fetch-Site": site}, **kw)
    assert r.status_code == 403 and err(r) == "cross_site_request_refused"


def test_the_refusal_happens_before_anything_is_stored(client, engine):
    client.post("/messages/text", json={"text": CLEAN_COMPLAINT}, headers={"Sec-Fetch-Site": "cross-site"})
    assert client.get("/messages").json()["total"] == 0


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_swagger_ui_and_the_address_bar_still_work(client, site):
    r = client.post("/messages/text", json={"text": CLEAN_COMPLAINT}, headers={"Sec-Fetch-Site": site})
    assert r.status_code == 201


def test_requests_without_the_header_are_not_browser_requests_and_pass(client):
    mid = import_text(client, CLEAN_COMPLAINT)  # what the Streamlit server, scripts and curl do
    assert client.post(f"/messages/{mid}/analyze").status_code == 201


@pytest.mark.parametrize("site", ["cross-site", "same-site", "same-origin", "none"])
def test_reads_are_never_blocked_by_this_guard(client, site):
    assert client.get("/health", headers={"Sec-Fetch-Site": site}).status_code == 200
