"""Consent bound to message + text + model + cost, atomic budget reservation and duplicate in-flight requests.

Offline: a fake provider, file databases for the concurrency tests, real threads.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from claude_helpers import (
    GOOD,
    FakeClaude,
    analyze_claude,
    api_error,
    claude_client,
    consent_for,
    enabled_settings,
    response,
)
from conftest import import_text
from export_helpers import complaint
from sqlalchemy import text

from ai_ops_hub.analysis.claude.models import ClaudeConsent
from ai_ops_hub.errors import ClaudeCostGuardError, ClaudeInProgressError


def err(r) -> dict:
    return r.json()["error"]


def rows(engine, sql: str, **p):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), p)]


# --- consent is bound to this message, this exact text, this model and this cost ------------------------------------------------


def test_the_preview_carries_the_content_fingerprint_of_the_stored_message(engine):
    with claude_client(engine, enabled_settings()) as (c, _f, _fac):
        mid = import_text(c, complaint(1))
        pv = c.get(f"/messages/{mid}/claude/preview").json()
        assert pv["content_fingerprint"] == rows(engine, "SELECT content_fingerprint FROM messages WHERE id=:m", m=mid)[0][0]


def test_a_consent_given_for_one_message_cannot_be_used_for_another(engine):
    with claude_client(engine, enabled_settings(), FakeClaude(response(GOOD))) as (c, fake, factory):
        a, b = import_text(c, complaint(1)), import_text(c, complaint(2))
        consent_a = consent_for(c, a)
        r = c.post(f"/messages/{b}/claude/analyze", json=consent_a)
        assert r.status_code == 422 and err(r)["code"] == "claude_consent_required" and "not message" in err(r)["message"]
        assert fake.calls == 0 and factory.built == []
        assert rows(engine, "SELECT COUNT(*) FROM analyses WHERE message_id=:m", m=b) == [(0,)]
        assert c.post(f"/messages/{a}/claude/analyze", json=consent_a).status_code == 201  # the right message works


@pytest.mark.parametrize("override, fragment", [
    ({"content_fingerprint": "0" * 32}, "text changed"),
    ({"message_id": 999}, "not message"),
    ({"model": "claude-sonnet-5-5"}, "configured model"),
    ({"accepted_max_cost_usd": 0.0001}, "below the worst-case estimate"),
])
def test_every_consent_parameter_must_match_the_request(engine, override, fragment):
    with claude_client(engine, enabled_settings(), FakeClaude(response(GOOD))) as (c, fake, factory):
        mid = import_text(c, complaint(1))
        r = c.post(f"/messages/{mid}/claude/analyze", json=consent_for(c, mid, **override))
        assert r.status_code == 422 and err(r)["code"] == "claude_consent_required" and fragment in err(r)["message"]
        assert fake.calls == 0 and factory.built == []


def test_a_consent_goes_stale_when_the_message_text_changes_after_the_preview(engine):
    with claude_client(engine, enabled_settings(), FakeClaude(response(GOOD))) as (c, fake, _fac):
        mid = import_text(c, complaint(1))
        consent = consent_for(c, mid)  # the user saw THIS text
        with engine.begin() as conn:  # the stored text changes behind the preview (messages are immutable by design)
            conn.execute(text("UPDATE messages SET body_text = body_text || ' changed', content_fingerprint = :f WHERE id = :m"),
                         {"f": "e" * 64, "m": mid})
        r = c.post(f"/messages/{mid}/claude/analyze", json=consent)
        assert r.status_code == 422 and "text changed" in err(r)["message"] and fake.calls == 0
        assert c.post(f"/messages/{mid}/claude/analyze", json=consent_for(c, mid)).status_code == 201  # a fresh preview works


@pytest.mark.parametrize("drop", ["message_id", "content_fingerprint"])
def test_a_consent_without_the_binding_fields_is_a_validation_error(engine, drop):
    with claude_client(engine, enabled_settings(), FakeClaude(response(GOOD))) as (c, fake, _fac):
        mid = import_text(c, complaint(1))
        body = consent_for(c, mid)
        body.pop(drop)
        r = c.post(f"/messages/{mid}/claude/analyze", json=body)
        assert r.status_code == 422 and err(r)["code"] == "validation_error" and fake.calls == 0


# --- the daily budget is reserved atomically; identical requests are not paid twice -------------------------------------------------


class Blocker:
    """A provider answer that waits until the test releases it: a request that is in flight."""

    def __init__(self, payload=GOOD) -> None:
        self.entered, self.release, self.payload = threading.Event(), threading.Event(), payload

    def __call__(self):
        self.entered.set()
        assert self.release.wait(10), "test forgot to release the provider"
        return response(self.payload)


def run_in_thread(fn, *args):
    box = {}

    def target():
        try:
            box["result"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target)
    t.start()
    return t, box


def service_of(c):
    return c.app.state.services.claude


def tight(**kw):
    return enabled_settings(claude_daily_budget_usd=0.003, **kw)  # room for ONE request (worst case about 0.0022)


def test_a_request_in_flight_reserves_its_worst_case_so_a_second_one_cannot_spend_the_same_budget(file_engine):
    blocker = Blocker()
    with claude_client(file_engine, tight(), FakeClaude(blocker)) as (c, fake, _fac):
        a, b = import_text(c, complaint(1)), import_text(c, complaint(2))
        svc = service_of(c)
        consent_a, consent_b = ClaudeConsent(**consent_for(c, a)), ClaudeConsent(**consent_for(c, b))
        t, box = run_in_thread(svc.analyze, a, consent_a)
        assert blocker.entered.wait(10)
        assert [r[0] for r in rows(file_engine, "SELECT status FROM llm_calls")] == ["reserved"]
        assert svc.status().spent_last_24h_usd == pytest.approx(0.00217, abs=1e-5)  # the reservation already counts
        with pytest.raises(ClaudeCostGuardError):
            svc.analyze(b, consent_b)
        assert fake.calls == 1
        blocker.release.set()
        t.join(10)
        assert box["result"].created is True and fake.calls == 1


def test_the_binding_budget_check_happens_inside_the_reservation_not_on_a_stale_read(file_engine):
    """Both requests read nothing spent at the same moment; only the atomic reservation stops the second one."""
    blocker = Blocker()
    with claude_client(file_engine, tight(), FakeClaude(blocker)) as (c, fake, _fac):
        a, b = import_text(c, complaint(1)), import_text(c, complaint(2))
        svc = service_of(c)
        consents = {a: ClaudeConsent(**consent_for(c, a)), b: ClaudeConsent(**consent_for(c, b))}
        svc._spent_24h = lambda session: 0.0  # every early read is stale
        t, box = run_in_thread(svc.analyze, a, consents[a])
        assert blocker.entered.wait(10)
        with pytest.raises(ClaudeCostGuardError) as info:
            svc.analyze(b, consents[b])
        assert "reserved or spent" in str(info.value)  # refused by the reservation, not by the early check
        blocker.release.set()
        t.join(10)
        assert fake.calls == 1 and "error" not in box


def test_two_simultaneous_requests_cannot_both_spend_the_same_remaining_budget(file_engine):
    heavy = response(GOOD, input_tokens=50_000, output_tokens=4_000)  # actual cost 0.007: over the 0.003 budget on its own
    with claude_client(file_engine, tight(), FakeClaude(heavy)) as (c, fake, _fac):
        a, b = import_text(c, complaint(1)), import_text(c, complaint(2))
        svc = service_of(c)
        consents = {a: ClaudeConsent(**consent_for(c, a)), b: ClaudeConsent(**consent_for(c, b))}
        barrier = threading.Barrier(2, timeout=10)
        real = svc._spent_24h

        def read_together(session):  # both threads read the budget at the same time, before either reserves
            value = real(session)
            barrier.wait()
            return value

        svc._spent_24h = read_together
        threads = [run_in_thread(svc.analyze, mid, consents[mid]) for mid in (a, b)]
        for t, _ in threads:
            t.join(15)
        outcomes = [box.get("result") or box.get("error") for _, box in threads]
        ok = [o for o in outcomes if not isinstance(o, BaseException)]
        refused = [o for o in outcomes if isinstance(o, ClaudeCostGuardError)]
        assert len(ok) == 1 and len(refused) == 1 and fake.calls == 1
        assert rows(file_engine, "SELECT COUNT(*) FROM llm_calls") == [(1,)]


def test_an_identical_request_for_the_same_message_is_not_sent_twice_while_the_first_runs(file_engine):
    blocker = Blocker()
    with claude_client(file_engine, enabled_settings(), FakeClaude(blocker)) as (c, fake, _fac):
        mid = import_text(c, complaint(1))
        svc = service_of(c)
        consent = ClaudeConsent(**consent_for(c, mid))
        t, box = run_in_thread(svc.analyze, mid, consent)
        assert blocker.entered.wait(10)
        with pytest.raises(ClaudeInProgressError):
            svc.analyze(mid, consent)
        assert fake.calls == 1
        blocker.release.set()
        t.join(10)
        assert box["result"].created is True
        assert svc.analyze(mid, consent).created is False and fake.calls == 1  # afterwards: stored, nothing sent


def test_in_progress_maps_to_a_409_error_code():
    from ai_ops_hub.api.errors import DOMAIN_ERRORS

    assert DOMAIN_ERRORS[ClaudeInProgressError] == (409, "claude_in_progress")


def test_the_reservation_row_is_settled_in_place_never_duplicated(engine):
    with claude_client(engine, enabled_settings(), FakeClaude(api_error("timeout"), response(GOOD))) as (c, _f, _fac):
        mid = import_text(c, complaint(1))
        assert analyze_claude(c, mid).status_code == 504
        assert [r[0] for r in rows(engine, "SELECT status FROM llm_calls")] == ["failed"]  # one row, settled
        assert analyze_claude(c, mid).status_code == 201
        assert [r[0] for r in rows(engine, "SELECT status FROM llm_calls ORDER BY id")] == ["failed", "ok"]


def test_a_reservation_that_never_settled_keeps_counting_but_stops_blocking_after_the_window(engine):
    now = {"t": datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)}
    with claude_client(engine, tight(), FakeClaude(response(GOOD)), clock=lambda: now["t"]) as (c, fake, _fac):
        mid = import_text(c, complaint(1))
        with engine.begin() as conn:  # a request that crashed mid-flight: reserved, never settled, 10 minutes ago
            conn.execute(text("INSERT INTO llm_calls (message_id, model, status, estimated_max_cost_usd, values_proposed, values_rejected, created_at)"
                              " VALUES (:m, 'claude-haiku-5-5', 'reserved', 0.0005, 0, 0, :t)"),
                         {"m": mid, "t": "2026-11-17 09:49:00"})
        assert analyze_claude(c, mid).status_code == 201  # not in flight any more (older than the window)
        assert c.get("/claude/status").json()["spent_last_24h_usd"] >= 0.0005  # but it still counted toward the budget


def test_a_fresh_unsettled_reservation_blocks_an_identical_request(engine):
    now = {"t": datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)}
    with claude_client(engine, enabled_settings(), FakeClaude(response(GOOD)), clock=lambda: now["t"]) as (c, fake, _fac):
        mid = import_text(c, complaint(1))
        consent = consent_for(c, mid)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO llm_calls (message_id, model, status, estimated_max_cost_usd, values_proposed, values_rejected, created_at)"
                              " VALUES (:m, 'claude-haiku-5-5', 'reserved', 0.0005, 0, 0, :t)"),
                         {"m": mid, "t": "2026-11-17 09:58:00"})
        r = c.post(f"/messages/{mid}/claude/analyze", json=consent)
        assert r.status_code == 409 and err(r)["code"] == "claude_in_progress" and fake.calls == 0


def test_the_database_only_accepts_the_three_usage_states(engine):
    from sqlalchemy.exc import IntegrityError

    with claude_client(engine, enabled_settings()) as (c, _f, _fac):
        mid = import_text(c, complaint(1))
        stmt = text("INSERT INTO llm_calls (message_id, model, status, estimated_max_cost_usd, values_proposed, values_rejected, created_at)"
                    " VALUES (:m, 'x', :s, 0.1, 0, 0, '2026-11-17 10:00:00')")
        for status in ("reserved", "ok", "failed"):
            with engine.begin() as conn:
                conn.execute(stmt, {"m": mid, "s": status})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(stmt, {"m": mid, "s": "bogus"})


def test_the_local_budget_is_documented_as_not_a_billing_guarantee():
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "not a guaranteed spending limit" in readme and "no hard spending limit" in readme.lower().replace("there is no hard spending limit", "no hard spending limit")
    assert "reserve" in readme.lower()
