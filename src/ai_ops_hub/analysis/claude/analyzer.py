"""ClaudeAnalyzer: the Analyzer protocol implemented with the official Anthropic SDK.

* One request, no tools, no web search, no streaming, no thinking configuration, no retries
  (``max_retries=0`` on the client the factory builds): a paid request is never repeated silently.
* Schema-constrained JSON (``output_config.format``); everything the model returns is then
  validated and verified locally (``verify.build_result``).
* Only the subject and body are sent. Prompts and responses are NEVER logged or stored; only
  token counts, latency, the request id and verification counters are kept.
* This class does not decide WHETHER a request may be sent: consent, enablement and cost guards
  belong to ``ClaudeService``. Constructing or importing it performs no network I/O.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import anthropic
from pydantic import ValidationError

from ai_ops_hub.analysis.claude.prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_content
from ai_ops_hub.analysis.claude.schema import RESPONSE_SCHEMA, RawAnalysis
from ai_ops_hub.analysis.claude.verify import VerificationStats, build_result
from ai_ops_hub.analysis.schemas import AnalysisInput, AnalysisResult
from ai_ops_hub.config import OFFICIAL_CLAUDE_BASE_URL
from ai_ops_hub.errors import (
    ClaudeAuthError,
    ClaudeBadResponseError,
    ClaudeProviderError,
    ClaudeRateLimitedError,
    ClaudeTimeoutError,
)
from ai_ops_hub.schemas import AnalysisMethod

log = logging.getLogger("ai_ops_hub.claude")

ANALYZER_NAME = "claude_analyzer"


@dataclass(frozen=True)
class CallInfo:
    """What the last request cost and how it went (never any email or model text)."""

    input_tokens: int
    output_tokens: int
    latency_ms: int
    request_id: str | None
    values_proposed: int
    values_accepted: int
    values_rejected: int
    values_downgraded: int
    rejection_reasons: tuple[str, ...]


def make_client(
    api_key: str, timeout_seconds: float, base_url: str = OFFICIAL_CLAUDE_BASE_URL, http_client: Any = None
) -> anthropic.Anthropic:
    """The only place a real client is built: no automatic retries, bounded time.

    ``base_url`` is ALWAYS passed explicitly: left to itself the SDK obeys the ANTHROPIC_BASE_URL environment variable
    and would send the API key to whatever address that variable names. ``http_client`` exists so tests can run the
    REAL SDK over a mocked transport (no network)."""
    extra = {} if http_client is None else {"http_client": http_client}
    return anthropic.Anthropic(api_key=api_key, base_url=base_url, max_retries=0, timeout=timeout_seconds, **extra)


class ClaudeAnalyzer:
    name = ANALYZER_NAME
    method = AnalysisMethod.LLM

    def __init__(self, client: Any, *, model: str, max_output_tokens: int) -> None:
        self._client = client
        self.model = model
        self._max_output_tokens = max_output_tokens
        self.version = f"{PROMPT_VERSION}/{model}"
        self.last_call: CallInfo | None = None

    def analyze(self, message: AnalysisInput) -> AnalysisResult:
        started = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_output_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_content(message.subject, message.body_text)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
            )
        except anthropic.APITimeoutError as exc:
            raise ClaudeTimeoutError("the Claude API did not answer in time") from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeRateLimitedError("the Claude API rate limit was reached; try again later") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ClaudeAuthError("the Claude API rejected the API key") from exc
        except anthropic.APIConnectionError as exc:
            raise ClaudeProviderError("could not connect to the Claude API") from exc
        except anthropic.APIStatusError as exc:
            raise ClaudeProviderError(f"the Claude API answered with an error (HTTP {exc.status_code})") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        request_id = getattr(response, "_request_id", None)
        try:
            raw = self._parse(response)
            result, stats = build_result(
                raw, message, analyzer_name=self.name, analyzer_version=self.version
            )
        except ClaudeBadResponseError as exc:
            # the request WAS billed: keep its usage so the caller can record it
            self.last_call = self._info(in_tok, out_tok, latency_ms, request_id, VerificationStats())
            raise ClaudeBadResponseError(str(exc), billed_input_tokens=in_tok) from exc
        self.last_call = self._info(in_tok, out_tok, latency_ms, request_id, stats)
        log.info("claude analysis done model=%s in=%d out=%d ms=%d request_id=%s",
                 self.model, in_tok, out_tok, latency_ms, request_id)
        return result

    # ------------------------------------------------------------------------------------

    @staticmethod
    def _info(in_tok: int, out_tok: int, ms: int, request_id: str | None, s: VerificationStats) -> CallInfo:
        return CallInfo(in_tok, out_tok, ms, request_id, s.proposed, s.accepted, s.rejected, s.downgraded,
                        tuple(s.reasons))

    @staticmethod
    def _parse(response: Any) -> RawAnalysis:
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            raise ClaudeBadResponseError("the Claude API declined to answer this request")
        if stop == "max_tokens":
            raise ClaudeBadResponseError("the answer was cut off (output token limit); nothing was stored")
        text = next((b.text for b in getattr(response, "content", []) if getattr(b, "type", None) == "text"), None)
        if not text:
            raise ClaudeBadResponseError("the Claude API answer contained no text")
        try:
            return RawAnalysis.model_validate(json.loads(text))
        except (ValueError, ValidationError) as exc:
            raise ClaudeBadResponseError("the Claude API answer did not match the required schema") from exc
