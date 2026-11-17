"""The REAL Anthropic SDK (installed version) over a MOCKED transport: no socket is ever opened.

This checks that what ClaudeAnalyzer sends is what the SDK and the documented Messages API accept:
model id, output_config.format (json_schema), output_config.effort, max_tokens, max_retries=0 and the
parsing of a structured-output response (content blocks, usage, request id, stop reasons).
Documentation facts used here (platform.claude.com, checked 2026-11-17): effort accepts low/medium/high/xhigh/max
on Haiku/Sonnet/Opus 5.5; structured outputs are `output_config.format = {"type": "json_schema", "schema": ...}`
and the JSON arrives in a text block; max_tokens/refusal can yield output that does not match the schema.
"""

from __future__ import annotations

import inspect
import json

import anthropic
import httpx2 as httpx
import pytest
from claude_helpers import FAKE_KEY, GOOD

from ai_ops_hub.analysis.claude import pricing
from ai_ops_hub.analysis.claude.analyzer import ClaudeAnalyzer, make_client
from ai_ops_hub.analysis.claude.schema import RESPONSE_SCHEMA
from ai_ops_hub.analysis.schemas import AnalysisInput
from ai_ops_hub.errors import (
    ClaudeAuthError,
    ClaudeBadResponseError,
    ClaudeProviderError,
    ClaudeRateLimitedError,
    ClaudeTimeoutError,
)

@pytest.fixture(autouse=True)
def _clean_sdk_environment(monkeypatch):
    """The SDK reads these variables; a developer machine may have them set. Tests must not depend on them."""
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)


DOCUMENTED_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}  # IDs from the pricing/models docs
DOCUMENTED_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
BODY = "Prosimy o korektę faktury FV/2026/0001 na kwotę 12 500,00 zł. Odpowiedź do 20 listopada 2026.\nAnna Wiśniewska"
MSG = AnalysisInput(subject="Sprzeciw FV/2026/0001", body_text=BODY, from_addr="secret@zielonadolina.example")


def message_json(payload=None, *, text=None, stop_reason="end_turn", blocks=None, model="claude-haiku-5-5", usage=None):
    content = blocks if blocks is not None else [{"type": "text", "text": text if text is not None else json.dumps(payload or GOOD)}]
    return {
        "id": "msg_01FAKE", "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": usage or {"input_tokens": 1500, "output_tokens": 220},
    }


class Wire:
    """A mocked HTTP layer that records every request the real SDK sends."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes) or [httpx.Response(200, json=message_json(), headers={"request-id": "req_wire_1"})]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.outcomes[min(len(self.requests) - 1, len(self.outcomes) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    def analyzer(self, model="claude-haiku-5-5", tokens=3000) -> ClaudeAnalyzer:
        http = anthropic.DefaultHttpxClient(transport=httpx.MockTransport(self))
        return ClaudeAnalyzer(make_client(FAKE_KEY, 30, http_client=http), model=model, max_output_tokens=tokens)

    @property
    def body(self) -> dict:
        return json.loads(self.requests[0].content)


# --- what goes over the wire ------------------------------------------------------------------------------------------


def test_the_installed_sdk_is_v1_and_messages_create_accepts_output_config():
    assert int(anthropic.__version__.split(".")[0]) >= 1
    assert "output_config" in inspect.signature(anthropic.resources.messages.Messages.create).parameters


def test_the_request_the_real_sdk_sends_matches_the_documented_messages_api():
    wire = Wire()
    wire.analyzer(tokens=3000).analyze(MSG)
    req = wire.requests[0]
    assert req.method == "POST" and req.url.path == "/v1/messages" and str(req.url).startswith("https://api.anthropic.com/")
    assert req.headers["x-api-key"] == FAKE_KEY and "anthropic-version" in req.headers
    body = wire.body
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 3000
    assert isinstance(body["system"], str) and body["system"].startswith("You extract structured business data")
    assert body["messages"] == [{"role": "user", "content": body["messages"][0]["content"]}]
    assert body["messages"][0]["content"].startswith("<email_subject>Sprzeciw FV/2026/0001</email_subject>")
    assert "secret@zielonadolina.example" not in req.content.decode("utf-8")  # the sender is never sent


def test_output_config_carries_a_json_schema_format_and_a_documented_effort():
    wire = Wire()
    wire.analyzer().analyze(MSG)
    cfg = wire.body["output_config"]
    assert set(cfg) == {"effort", "format"}
    assert cfg["effort"] == "low" and cfg["effort"] in DOCUMENTED_EFFORTS
    assert cfg["format"] == {"type": "json_schema", "schema": RESPONSE_SCHEMA}
    assert cfg["format"]["schema"]["additionalProperties"] is False  # required for objects by the structured-output rules


def test_the_request_has_no_tools_thinking_sampling_or_streaming_parameters():
    wire = Wire()
    wire.analyzer().analyze(MSG)
    assert set(wire.body) == {"model", "max_tokens", "system", "messages", "output_config"}
    for forbidden in ("tools", "tool_choice", "thinking", "temperature", "top_p", "top_k", "stream", "mcp_servers", "betas"):
        assert forbidden not in wire.body


@pytest.mark.parametrize("model", sorted(pricing.PRICES))
def test_every_priced_model_id_is_a_documented_id_and_is_sent_verbatim(model):
    assert model in DOCUMENTED_MODELS
    assert not model.rsplit("-", 1)[-1].isdigit() or len(model.rsplit("-", 1)[-1]) < 8  # never a date-suffixed id
    wire = Wire()
    wire.analyzer(model=model).analyze(MSG)
    assert wire.body["model"] == model
    assert wire.analyzer(model=model).version == f"1.0/{model}"


def test_the_configured_default_model_and_the_price_table_agree():
    from ai_ops_hub.config import Settings

    assert Settings(_env_file=None).claude_model in pricing.PRICES and set(pricing.PRICES) == DOCUMENTED_MODELS


def test_max_tokens_is_the_configured_cap():
    for cap in (256, 4000, 16000):
        wire = Wire()
        wire.analyzer(tokens=cap).analyze(MSG)
        assert wire.body["max_tokens"] == cap


# --- retries and failures with the real SDK ----------------------------------------------------------------------------


@pytest.mark.parametrize("response, exc_type", [
    (httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "secret detail"}}), ClaudeProviderError),
    (httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}), ClaudeProviderError),
    (httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, headers={"retry-after": "1"}), ClaudeRateLimitedError),
    (httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}), ClaudeAuthError),
    (httpx.Response(403, json={"type": "error", "error": {"type": "permission_error", "message": "no"}}), ClaudeAuthError),
    (httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "nope"}}), ClaudeProviderError),
])
def test_the_real_sdk_with_max_retries_zero_makes_exactly_one_request_per_failure(response, exc_type):
    wire = Wire(response, httpx.Response(200, json=message_json()))  # a second answer that would succeed if retried
    with pytest.raises(exc_type) as info:
        wire.analyzer().analyze(MSG)
    assert len(wire.requests) == 1
    assert "secret detail" not in str(info.value) and FAKE_KEY not in str(info.value)


@pytest.mark.parametrize("failure, exc_type", [
    (httpx.ReadTimeout("slow"), ClaudeTimeoutError), (httpx.ConnectTimeout("slow"), ClaudeTimeoutError),
    (httpx.ConnectError("refused"), ClaudeProviderError),
])
def test_network_failures_are_not_retried_either(failure, exc_type):
    wire = Wire(failure, httpx.Response(200, json=message_json()))
    with pytest.raises(exc_type):
        wire.analyzer().analyze(MSG)
    assert len(wire.requests) == 1


def test_make_client_without_a_test_transport_has_retries_off_and_the_given_timeout():
    client = make_client(FAKE_KEY, 17.5)
    assert client.max_retries == 0 and client.timeout == 17.5 and client.base_url.host == "api.anthropic.com"


# --- parsing a real structured-output response -------------------------------------------------------------------------


def test_a_structured_output_response_is_parsed_into_a_verified_analysis_with_usage_and_request_id():
    wire = Wire(httpx.Response(200, json=message_json(usage={"input_tokens": 1500, "output_tokens": 220}), headers={"request-id": "req_wire_9"}))
    a = wire.analyzer()
    result = a.analyze(MSG)
    assert {f.name.value for f in result.fields} == {"reference", "amount", "deadline", "contact_name"}
    assert (a.last_call.input_tokens, a.last_call.output_tokens, a.last_call.request_id) == (1500, 220, "req_wire_9")


def test_a_thinking_block_before_the_text_block_is_skipped():
    blocks = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": json.dumps(GOOD)}]
    result = Wire(httpx.Response(200, json=message_json(blocks=blocks))).analyzer().analyze(MSG)
    assert len(result.fields) == 4


def test_usage_with_cache_fields_and_extra_keys_is_tolerated():
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
             "service_tier": "standard", "future_field": 1}
    a = Wire(httpx.Response(200, json=message_json(usage=usage))).analyzer()
    a.analyze(MSG)
    assert (a.last_call.input_tokens, a.last_call.output_tokens) == (10, 5)


@pytest.mark.parametrize("kwargs, fragment", [
    ({"stop_reason": "max_tokens"}, "cut off"),
    ({"stop_reason": "refusal"}, "declined"),
    ({"text": "{not json"}, "schema"),
    ({"text": json.dumps({"category": "order"})}, "schema"),
    ({"text": json.dumps({"category": "weird", "category_alternatives": [], "fields": []})}, "schema"),
    ({"blocks": []}, "no text"),
])
def test_incomplete_or_invalid_structured_output_is_refused_and_billed_usage_is_kept(kwargs, fragment):
    wire = Wire(httpx.Response(200, json=message_json(**kwargs)))
    a = wire.analyzer()
    with pytest.raises(ClaudeBadResponseError) as info:
        a.analyze(MSG)
    assert fragment in str(info.value) and info.value.billed_input_tokens == 1500 and len(wire.requests) == 1


def test_no_real_api_call_is_possible_in_these_tests():
    import socket

    with pytest.raises(RuntimeError, match="blocked"):
        socket.socket().connect(("api.anthropic.com", 443))
