"""The launchers refuse unsafe bindings: the API has no authentication."""

from __future__ import annotations

import pytest

from ai_ops_hub.api import __main__ as api_main


@pytest.fixture(autouse=True)
def _no_server(monkeypatch):
    started = []
    monkeypatch.setattr(api_main.uvicorn, "run", lambda *a, **k: started.append((a, k)))
    monkeypatch.setattr(api_main, "create_app", lambda **k: object())
    monkeypatch.setenv("AIOPS_DATABASE_URL", "sqlite://")
    api_main.get_settings.cache_clear()
    yield started
    api_main.get_settings.cache_clear()


def test_the_api_listens_on_loopback_by_default(_no_server, capsys):
    assert api_main.main([]) == 0 and _no_server[0][1]["host"] == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_the_api_refuses_a_non_local_address_without_the_explicit_flag(host, _no_server, capsys):
    assert api_main.main(["--host", host]) == 2 and _no_server == []
    assert "no authentication" in capsys.readouterr().out


def test_the_explicit_flag_allows_a_remote_address_when_claude_is_off(_no_server):
    assert api_main.main(["--host", "0.0.0.0", "--allow-remote"]) == 0 and len(_no_server) == 1


@pytest.mark.parametrize("flag", [[], ["--allow-remote"]])
def test_the_api_never_listens_remotely_while_the_paid_claude_analysis_is_enabled(flag, _no_server, monkeypatch, capsys):
    monkeypatch.setenv("AIOPS_CLAUDE_ENABLED", "true")
    api_main.get_settings.cache_clear()
    assert api_main.main(["--host", "0.0.0.0", *flag]) == 2 and _no_server == []
    out = capsys.readouterr().out
    assert "refusing to listen" in out
    if flag:  # even the explicit override cannot expose the paid endpoint
        assert "Claude analysis is enabled" in out


def test_loopback_with_claude_enabled_is_fine(_no_server, monkeypatch):
    monkeypatch.setenv("AIOPS_CLAUDE_ENABLED", "true")
    api_main.get_settings.cache_clear()
    assert api_main.main([]) == 0
