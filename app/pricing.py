"""Estimated OpenAI API cost of ripgpt usage.

ripgpt drives the ChatGPT web UI, so it costs nothing per call — but it's useful to know
what the SAME traffic would have cost on the paid API. Prices are USD per 1,000,000 tokens
(Standard processing tier, from openai.com/business/pricing, captured 2026-06-29).

Edit PRICES / _ALIAS / IMAGE_PRICE to keep this current. Caveats (shown to the user too):
  * We can't distinguish cached vs fresh input tokens, so ALL prompt tokens are billed at
    the full input rate — this is an UPPER BOUND (real API cost with caching is lower).
  * Models with no public per-token price (gpt-5.3, o3, the account-default "chatgpt") are
    mapped to the nearest published tier — clearly an estimate.
  * gpt-image output is billed per generated image (IMAGE_PRICE), not per token.
"""

from __future__ import annotations

# USD per 1,000,000 tokens — published Standard prices.
PRICES: dict[str, dict[str, float]] = {
    "gpt-5.5":      {"in": 5.00, "out": 30.00},
    "gpt-5.4":      {"in": 2.50, "out": 15.00},
    "gpt-5.4-mini": {"in": 0.75, "out": 4.50},
}

# Map ripgpt resolved model slugs (metrics' "model_res") → a price tier above.
_ALIAS: dict[str, str] = {
    "gpt-5-5":          "gpt-5.5",
    "gpt-5-5-instant":  "gpt-5.5",
    "gpt-5-5-thinking": "gpt-5.5",
    "gpt-5-4-thinking": "gpt-5.4",
    "gpt-5-3":          "gpt-5.4",   # no public 5.3 price → nearest tier (estimate)
    "o3":               "gpt-5.4",   # approximate
    "chatgpt":          "gpt-5.5",   # persistent chat = account default model
    "auto":             "gpt-5.5",
}

DEFAULT_TIER = "gpt-5.5"

# USD per generated image (gpt-image-1, ~1024² standard quality) — estimate.
IMAGE_PRICE = 0.04


def tier_for(model_res: str | None) -> str:
    if not model_res:
        return DEFAULT_TIER
    if model_res in PRICES:
        return model_res
    return _ALIAS.get(model_res, DEFAULT_TIER)


def cost_for(model_res: str | None, ptoks: int, ctoks: int, images: int = 0) -> float:
    """Estimated USD the given usage would have cost on the paid API."""
    p = PRICES[tier_for(model_res)]
    cost = (ptoks / 1_000_000.0) * p["in"] + (ctoks / 1_000_000.0) * p["out"]
    cost += int(images) * IMAGE_PRICE
    return round(cost, 6)
