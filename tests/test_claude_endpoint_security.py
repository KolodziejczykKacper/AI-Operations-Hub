"""The API key must only ever go to the endpoint the operator configured (default: api.anthropic.com).

The Anthropic SDK obeys the ANTHROPIC_BASE_URL environment variable, so a stray or malicious value would
silently redirect the key and the email. These tests prove the application ignores that variable.
Offline: a mocked transport, no sockets.
"""

from __future__ import annotations

from pathlib import Path

import anthropic
import httpx2 as httpx
import pytest
from claude_helpers import FAKE_KEY, GOOD, FakeClaude, analyze_claude, claude_client, enabled_settings, response
from conftest import import_text
from export_helpers import complaint
from fastapi.testclient import TestClient
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from ai_ops_hub.analysis.claude.analyzer import make_client
from ai_ops_hub.api import create_app
from ai_ops_hub.config import OFFICIAL_CLAUDE_BASE_URL, Settings

EVIL = "https://evil.example.invalid"
APP = Path(__file__).resolve().parents[1] / "src" / "ai_ops_hub" / "ui" / "streamlit_app.py"


@pytest.fixture(autouse=True)
def _poisoned_environment(monkeypatch):
    """Simulate a hostile or mistaken environment for every test in this file."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", EVIL)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AIOPS_CLAUDE_BASE_URL", raising=False)
    monkeypatch.delenv("AIOPS_CLAUDE_ALLOW_CUSTOM_BASE_URL", raising=False)


def message_body() -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": '{"category":"other","category_alternatives":[],"fields":[]}'}],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}


def wire(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=message_body())

    return anthropic.DefaultHttpxClient(transport=httpx.MockTransport(handler))


def test_the_raw_sdk_would_follow_the_environment_which_is_the_risk_being_closed():
    """Documents WHY make_client sets the address explicitly: the bare SDK sends the key to the environment's host."""
    seen: list = []
    client = anthropic.Anthropic(api_key=FAKE_KEY, http_client=wire(seen))
    assert client.base_url.host == "evil.example.invalid"
    client.messages.create(model="claude-haiku-5-5", max_tokens=5, messages=[{"role": "user", "content": "x"}])
    assert seen[0].url.host == "evil.example.invalid" and seen[0].headers["x-api-key"] == FAKE_KEY


def test_make_client_ignores_the_base_url_environment_variable():
    seen: list = []
    client = make_client(FAKE_KEY, 5, http_client=wire(seen))
    assert client.base_url.host == "api.anthropic.com"
    client.messages.create(model="claude-haiku-5-5", max_tokens=5, messages=[{"role": "user", "content": "x"}])
    assert seen[0].url.host == "api.anthropic.com" and "evil" not in str(seen[0].url)


def test_the_default_application_client_goes_to_the_official_host_even_with_a_poisoned_environment(engine, monkeypatch):
    built = []
    real = make_client

    def spy(api_key, timeout, base_url=OFFICIAL_CLAUDE_BASE_URL, http_client=None):
        built.append(base_url)
        seen: list = []
        built.append(seen)
        return real(api_key, timeout, base_url, http_client=wire(seen))  # real SDK, mocked transport

    monkeypatch.setattr("ai_ops_hub.api.app.make_client", spy)
    with TestClient(create_app(settings=enabled_settings(), engine=engine)) as c:  # NO factory override
        mid = import_text(c, complaint(1))
        assert analyze_claude(c, mid).status_code == 201
    assert built[0] == OFFICIAL_CLAUDE_BASE_URL
    assert [r.url.host for r in built[1]] == ["api.anthropic.com"]


def test_the_endpoint_in_the_environment_is_not_even_read_by_the_settings():
    s = enabled_settings()
    assert s.claude_base_url == OFFICIAL_CLAUDE_BASE_URL and EVIL not in repr(s)


# --- the configured endpoint is validated ----------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "http://api.anthropic.com", "ftp://api.anthropic.com", "api.anthropic.com", "https://", "",
    "https://user:pass@api.anthropic.com", "https://api.anthropic.com?x=1", "https://api.anthropic.com#frag",
])
def test_the_base_url_must_be_a_plain_https_url(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, claude_base_url=bad)


def test_a_non_official_host_needs_an_explicit_opt_in():
    with pytest.raises(ValidationError, match="ALLOW_CUSTOM_BASE_URL"):
        Settings(_env_file=None, claude_base_url="https://proxy.corp.example")
    s = Settings(_env_file=None, claude_base_url="https://proxy.corp.example/", claude_allow_custom_base_url=True)
    assert s.claude_base_url == "https://proxy.corp.example"  # trailing slash normalised


def test_a_lookalike_host_is_not_treated_as_official():
    for host in ("https://api.anthropic.com.evil.example", "https://evil-api.anthropic.com.example", "https://api.anthropic.co"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, claude_base_url=host)


def test_the_custom_address_can_come_from_the_aiops_environment_only(monkeypatch):
    monkeypatch.setenv("AIOPS_CLAUDE_BASE_URL", "https://proxy.corp.example")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("AIOPS_CLAUDE_ALLOW_CUSTOM_BASE_URL", "true")
    assert Settings(_env_file=None).claude_base_url == "https://proxy.corp.example"


def test_a_custom_endpoint_reaches_the_sdk_only_when_configured_and_is_reported_to_the_user(engine, monkeypatch):
    got = []

    def spy(api_key, timeout, base_url=OFFICIAL_CLAUDE_BASE_URL, http_client=None):
        got.append(base_url)
        return FakeClaude(response(GOOD))

    monkeypatch.setattr("ai_ops_hub.api.app.make_client", spy)
    custom = enabled_settings(claude_base_url="https://proxy.corp.example", claude_allow_custom_base_url=True)
    with TestClient(create_app(settings=custom, engine=engine)) as c:
        mid = import_text(c, complaint(1))
        st = c.get("/claude/status").json()
        pv = c.get(f"/messages/{mid}/claude/preview").json()
        assert (st["endpoint_host"], st["custom_endpoint"]) == ("proxy.corp.example", True)
        assert (pv["endpoint_host"], pv["custom_endpoint"]) == ("proxy.corp.example", True)
        assert analyze_claude(c, mid).status_code == 201
    assert got == ["https://proxy.corp.example"]


def test_the_default_status_and_preview_name_the_official_host(engine):
    with claude_client(engine, enabled_settings()) as (c, _f, _fac):
        mid = import_text(c, complaint(1))
        assert c.get("/claude/status").json()["endpoint_host"] == "api.anthropic.com"
        pv = c.get(f"/messages/{mid}/claude/preview").json()
        assert pv["endpoint_host"] == "api.anthropic.com" and pv["custom_endpoint"] is False


# --- the user sees where the data goes -------------------------------------------------------------------------------------------


def start(rec_client, message=1):
    from ai_ops_hub.ui.client import ApiClient

    at = AppTest.from_file(str(APP), default_timeout=30)
    at.session_state["client"] = ApiClient("http://127.0.0.1:8000", http=rec_client)
    at.session_state["page"] = "Analiza Claude"
    at.session_state["cl_message"] = message
    return at.run()


def texts(at) -> str:
    out = []
    for kind in ("markdown", "caption", "warning", "error", "info", "success"):
        out += [str(e.value) for e in getattr(at, kind)]
    return "\n".join(out).replace("\\", "")


def test_the_consent_screen_names_the_official_destination(engine):
    with claude_client(engine, enabled_settings()) as (c, _f, _fac):
        import_text(c, complaint(1))
        text = texts(start(c))
        assert "Adres docelowy: `api.anthropic.com`" in text and "UWAGA" not in text and "(oficjalny)" in text


def test_a_custom_destination_gets_a_loud_warning_on_the_consent_screen(engine):
    custom = enabled_settings(claude_base_url="https://proxy.corp.example", claude_allow_custom_base_url=True)
    with claude_client(engine, custom) as (c, _f, _fac):
        import_text(c, complaint(1))
        text = texts(start(c))
        assert "UWAGA: skonfigurowano niestandardowy adres API" in text and "proxy.corp.example" in text
        assert "niestandardowy!" in text


def test_no_source_file_hardcodes_a_foreign_anthropic_endpoint_or_reads_the_sdk_env_var():
    root = Path(__file__).resolve().parents[1] / "src"
    for path in root.rglob("*.py"):
        text_ = path.read_text(encoding="utf-8")
        assert "ANTHROPIC_BASE_URL" not in text_.replace("ANTHROPIC_BASE_URL environment variable", "").replace(
            "ANTHROPIC_BASE_URL env", ""), path
