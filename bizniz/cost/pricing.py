"""
Hardcoded pricing table for the AI providers we use, keyed by the resolved
model name. Prices are USD per 1,000,000 tokens.

Update this table when providers change pricing or we add new models.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


# USD per 1M tokens. {input, output}
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # ── Google Gemini ─────────────────────────────────────────────────────────
    "gemini-2.5-flash-lite":          {"input": 0.10,  "output": 0.40},
    "gemini-2.5-flash":               {"input": 0.30,  "output": 2.50},
    "gemini-2.5-pro":                 {"input": 1.25,  "output": 10.00},
    "gemini-3.1-flash-lite-preview":  {"input": 0.10,  "output": 0.40},
    "gemini-3-flash-preview":         {"input": 0.30,  "output": 2.50},
    "gemini-3.1-pro-preview":         {"input": 1.25,  "output": 10.00},

    # ── OpenAI ────────────────────────────────────────────────────────────────
    "gpt-4o-mini":  {"input": 0.15,  "output": 0.60},
    "gpt-4o":       {"input": 2.50,  "output": 10.00},
    "gpt-5":        {"input": 1.25,  "output": 10.00},

    # ── Anthropic Claude ──────────────────────────────────────────────────────
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-7":            {"input": 15.00, "output": 75.00},
    # Common shorthand aliases the config layer uses
    "claude-sonnet":              {"input": 3.00,  "output": 15.00},
    "claude-opus":                {"input": 15.00, "output": 75.00},
    "claude-haiku":               {"input": 0.80,  "output": 4.00},
}


# Aliases used by BiznizConfig / GeminiClient that resolve to a real model name
_ALIASES: Dict[str, str] = {
    "gemini-flash-lite":  "gemini-2.5-flash-lite",
    "gemini-flash":       "gemini-3.1-flash-lite-preview",
    "gemini-flash-top":   "gemini-3-flash-preview",
    "gemini-pro":         "gemini-3.1-pro-preview",
}


@dataclass
class CallCost:
    """Resolved $ cost for one AI call."""
    input_cost: float    # USD
    output_cost: float   # USD
    total_cost: float    # USD
    model: str           # the resolved model name we priced against
    priced: bool         # False when no entry was found and we billed $0


def resolve_model(model: str) -> str:
    """Resolve a possibly-aliased model name to a canonical pricing-table key."""
    return _ALIASES.get(model, model)


def price_call(model: str, input_tokens: int, output_tokens: int) -> CallCost:
    """
    Return the USD cost for a single AI call.

    Unknown models price at $0 with ``priced=False`` so the caller can
    distinguish "this model is free" from "we don't have pricing data".
    """
    resolved = resolve_model(model)
    pricing = MODEL_PRICING.get(resolved)
    if pricing is None:
        return CallCost(input_cost=0.0, output_cost=0.0, total_cost=0.0,
                        model=resolved, priced=False)

    input_cost = (input_tokens / 1_000_000.0) * pricing["input"]
    output_cost = (output_tokens / 1_000_000.0) * pricing["output"]
    return CallCost(
        input_cost=input_cost,
        output_cost=output_cost,
        total_cost=input_cost + output_cost,
        model=resolved,
        priced=True,
    )
