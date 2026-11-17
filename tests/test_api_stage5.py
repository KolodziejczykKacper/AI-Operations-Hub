"""Stage 5 additions to the API: exact stats, subject/sender search, imported_at."""

from __future__ import annotations

import pytest
from conftest import AMBIGUOUS_ORDER, CLEAN_COMPLAINT, import_text, open_case


def _mail(subject: str, n: int, sender: str = "a@zielonadolina.example") -> str:
    return f"From: {sender}\nSubject: {subject}\nMessage-ID: <s5-{n}@x.example>\n\nPlease send us the invoice FV/2026/{n:04d}."


def test_stats_on_an_empty_database(client):
    r = client.get("/cases/stats")
    assert r.status_code == 200
    assert r.json() == {
        "total": 0,
        "by_status": {"needs_review": 0, "ready_for_review": 0, "approved": 0, "rejected": 0},
    }


def test_stats_count_every_status(client):
    clean = open_case(client, CLEAN_COMPLAINT)  # ready_for_review
    messy = open_case(client, AMBIGUOUS_ORDER)  # needs_review
    third = open_case(client, _mail("third", 3))
    client.post(f"/cases/{clean['id']}/approve", json={"expected_version": 1})
    client.post(f"/cases/{messy['id']}/reject", json={"expected_version": 1, "reason": "spam"})
    stats = client.get("/cases/stats").json()
    assert stats["total"] == 3
    assert stats["by_status"]["approved"] == 1 and stats["by_status"]["rejected"] == 1
    assert sum(stats["by_status"].values()) == stats["total"]
    assert third["status"] in stats["by_status"]


def test_stats_are_exact_whatever_the_page_size(client):
    for i in range(7):
        open_case(client, _mail(f"case {i}", i))
    assert len(client.get("/cases", params={"limit": 2}).json()["items"]) == 2
    assert client.get("/cases/stats").json()["total"] == 7  # not the page length


def test_stats_follow_state_changes(client):
    case = open_case(client, CLEAN_COMPLAINT)
    before = client.get("/cases/stats").json()["by_status"]
    client.post(f"/cases/{case['id']}/approve", json={"expected_version": 1})
    after = client.get("/cases/stats").json()["by_status"]
    assert before["ready_for_review"] == 1 and after["ready_for_review"] == 0 and after["approved"] == 1


def test_stats_route_is_not_shadowed_by_the_case_id_route(client):
    assert client.get("/cases/stats").status_code == 200
    assert client.get("/cases/stats").json().get("total") is not None


def test_search_by_subject_is_case_insensitive_and_counts_matches_only(client):
    open_case(client, _mail("Invoice dispute ALPHA", 1))
    open_case(client, _mail("Delivery question", 2))
    open_case(client, _mail("alpha follow-up", 3))
    page = client.get("/cases", params={"q": "alpha"}).json()
    assert page["total"] == 2 and {c["subject"] for c in page["items"]} == {"Invoice dispute ALPHA", "alpha follow-up"}


def test_search_by_sender(client):
    open_case(client, _mail("one", 1, sender="anna@zielonadolina.example"))
    open_case(client, _mail("two", 2, sender="piotr@fabryka.example"))
    page = client.get("/cases", params={"q": "fabryka"}).json()
    assert page["total"] == 1 and page["items"][0]["subject"] == "two"


def test_search_combines_with_status_and_pagination(client):
    for i in range(5):
        open_case(client, _mail(f"report {i}", i))
    open_case(client, _mail("other", 9))
    page = client.get("/cases", params={"q": "report", "limit": 2, "offset": 2}).json()
    assert page["total"] == 5 and len(page["items"]) == 2
    assert client.get("/cases", params={"q": "report", "status": "approved"}).json()["total"] == 0


@pytest.mark.parametrize("q", ["%", "_", "100%", "a_b", "\\", "'; DROP TABLE cases; --"])
def test_search_wildcards_and_sql_are_plain_text(client, q):
    open_case(client, _mail("plain subject", 1))
    r = client.get("/cases", params={"q": q})
    assert r.status_code == 200 and r.json()["total"] == 0
    assert client.get("/cases/stats").json()["total"] == 1


def test_a_literal_percent_sign_can_be_searched(client):
    open_case(client, _mail("discount 50% off", 1))
    open_case(client, _mail("discount 500 off", 2))
    assert client.get("/cases", params={"q": "50%"}).json()["total"] == 1


@pytest.mark.parametrize("q", ["", "x" * 101])
def test_search_query_bounds(client, q):
    assert client.get("/cases", params={"q": q}).status_code == 422


def test_case_summaries_carry_the_import_date(client):
    case = open_case(client, CLEAN_COMPLAINT)
    mid = case["message_id"]
    imported = client.get(f"/messages/{mid}").json()["message"]["imported_at"]
    assert case["imported_at"] == imported
    assert client.get("/cases").json()["items"][0]["imported_at"] == imported
    assert client.get(f"/cases/{case['id']}").json()["case"]["imported_at"] == imported


def test_existing_clients_are_unaffected_by_the_additions(client):
    case = open_case(client, CLEAN_COMPLAINT)
    page = client.get("/cases").json()
    assert set(page) == {"items", "total", "limit", "offset"}
    assert {"id", "message_id", "status", "version", "subject"} <= set(page["items"][0])
    assert import_text(client, CLEAN_COMPLAINT) == case["message_id"]
