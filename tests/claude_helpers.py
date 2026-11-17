"""Offline fakes for the Claude API. No network, no API key: every request goes to a fake client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx2 as httpx

from ai_ops_hub.config import Settings

FAKE_KEY = "sk-ant-FAKE-test-key-not-real"


def raw_field(name, quote, normalized=None, source="body", status="valid", note=""):
    return {"name": name, "quote": quote, "source": source,
            "normalized": quote if normalized is None else normalized, "status": status, "note": note}


def answer(category="order", fields=(), alternatives=()):
    return {"category": category, "category_alternatives": list(alternatives), "fields": list(fields)}


def response(payload, *, stop_reason="end_turn", input_tokens=900, output_tokens=150, request_id="req_fake_1", raw_text=None):
    text = raw_text if raw_text is not None else json.dumps(payload)
    r = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    r._request_id = request_id
    return r


class FakeMessages:
    def __init__(self, owner) -> None:
        self._owner = owner

    def create(self, **kwargs):
        owner = self._owner
        owner.requests.append(kwargs)
        item = owner.script[min(len(owner.requests) - 1, len(owner.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item() if callable(item) else item


class FakeClaude:
    """Stands in for ``anthropic.Anthropic``; records every request so tests can inspect it."""

    def __init__(self, *script) -> None:
        self.script = list(script) or [response(answer())]
        self.requests: list[dict] = []
        self.messages = FakeMessages(self)

    @property
    def calls(self) -> int:
        return len(self.requests)


class Factory:
    """The client factory the service receives; remembers what it was asked to build."""

    def __init__(self, client: FakeClaude) -> None:
        self.client = client
        self.built: list[tuple[str, float]] = []

    def __call__(self, api_key: str, timeout: float):
        self.built.append((api_key, timeout))
        return self.client


def enabled_settings(**over) -> Settings:
    base = dict(_env_file=None, claude_enabled=True, ANTHROPIC_API_KEY=FAKE_KEY)
    base.update(over)
    return Settings(**base)


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def api_error(kind: str):
    """Real SDK exception objects (constructed offline) for the failure paths."""
    req = _request()
    if kind == "timeout":
        return anthropic.APITimeoutError(request=req)
    if kind == "connection":
        return anthropic.APIConnectionError(request=req)
    status = {"rate_limit": 429, "auth": 401, "forbidden": 403, "server": 500, "overloaded": 529, "bad_request": 400}[kind]
    resp = httpx.Response(status, request=req, json={"error": {"type": "x", "message": "secret provider detail"}})
    cls = {429: anthropic.RateLimitError, 401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError,
           400: anthropic.BadRequestError}.get(status, anthropic.APIStatusError)
    return cls("provider said: secret provider detail", response=resp, body=None)


# --- wiring helpers --------------------------------------------------------------------------------------------

from contextlib import contextmanager  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from ai_ops_hub.api import create_app  # noqa: E402


@contextmanager
def claude_client(engine, settings, fake: FakeClaude | None = None, clock=None):
    """TestClient on a real app whose Claude client factory returns ``fake`` (never the real SDK)."""
    fake = fake or FakeClaude()
    factory = Factory(fake)
    kwargs = {"clock": clock} if clock is not None else {}
    with TestClient(create_app(settings=settings, engine=engine, claude_client_factory=factory, **kwargs)) as c:
        yield c, fake, factory


def consent_for(client, mid: int, **over) -> dict:
    """The consent body a user would send after reading the preview."""
    pv = client.get(f"/messages/{mid}/claude/preview").json()
    body = {"message_id": mid, "content_fingerprint": pv["content_fingerprint"], "confirm_send_to_anthropic": True,
            "model": pv["model"], "accepted_max_cost_usd": pv["estimated_max_cost_usd"]}
    body.update(over)
    return body


def analyze_claude(client, message_id: int, **over):
    return client.post(f"/messages/{message_id}/claude/analyze", json=consent_for(client, message_id, **over))


GOOD = answer("complaint", [
    raw_field("reference", "FV/2026/0001"),
    raw_field("amount", "12 500,00 zł", "12500.00 PLN"),
    raw_field("deadline", "20 listopada 2026", "2026-11-20"),
    raw_field("contact_name", "Anna Wiśniewska"),
])
