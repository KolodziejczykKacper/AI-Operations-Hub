"""Local price table and conservative cost estimates.

Prices are USD per 1M tokens as published by Anthropic on ``PRICES_VERIFIED_ON``. They can
change; a local estimate is a safety guard and NOT a guaranteed spending limit. Set billing
limits in the Anthropic Console as well.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

PRICES_VERIFIED_ON = "2026-11-17"
PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"

# Tokens the fixed instructions + JSON schema add to every request (rounded up on purpose).
PROMPT_OVERHEAD_TOKENS = 1_600
# Deliberately pessimistic: real text is ~3-4 characters per token; assume 2 (Polish, codes, numbers).
CHARS_PER_TOKEN = 2


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    long_threshold: int | None = None  # prompts above this many input tokens use the long prices
    long_input_per_mtok: Decimal | None = None
    long_output_per_mtok: Decimal | None = None


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(
        Decimal("0.10"), Decimal("0.50"), 100_000, Decimal("0.50"), Decimal("2.50")
    ),
    "claude-sonnet-5-5": ModelPrice(Decimal("2"), Decimal("10")),
    "claude-opus-5-5": ModelPrice(Decimal("4"), Decimal("20")),
}


def known_model(model: str) -> bool:
    return model in PRICES


def estimate_input_tokens(chars: int) -> int:
    return PROMPT_OVERHEAD_TOKENS + math.ceil(chars / CHARS_PER_TOKEN)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Cost of a request with these token counts (raises KeyError for an unknown model)."""
    p = PRICES[model]
    if p.long_threshold is not None and input_tokens > p.long_threshold:
        in_price, out_price = p.long_input_per_mtok, p.long_output_per_mtok
    else:
        in_price, out_price = p.input_per_mtok, p.output_per_mtok
    return (Decimal(input_tokens) * in_price + Decimal(output_tokens) * out_price) / Decimal(1_000_000)


def worst_case_cost_usd(model: str, chars: int, max_output_tokens: int) -> tuple[int, Decimal]:
    """(estimated input tokens, upper-bound cost): input estimate plus ALL output tokens used."""
    tokens = estimate_input_tokens(chars)
    return tokens, cost_usd(model, tokens, max_output_tokens)
