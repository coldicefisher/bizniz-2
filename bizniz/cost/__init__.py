"""
Cost tracking for AI client calls.

Each AI client records token usage to a shared :class:`CostTracker` after
every call. The tracker holds an in-memory log plus a roll-up summary
per model and per agent, and optionally persists rows to the workspace
SQLite database for cross-run analysis.

Public API::

    from bizniz.cost import get_tracker, price_call, MODEL_PRICING

    tracker = get_tracker()
    tracker.record(agent="autocoder", model="gemini-flash",
                   input_tokens=1500, output_tokens=800, duration_ms=3400)
    summary = tracker.summary()

The pricing table is hardcoded in ``pricing.py``; update it as provider
prices change.
"""
from bizniz.cost.pricing import MODEL_PRICING, CallCost, price_call, resolve_model
from bizniz.cost.tracker import CallRecord, CostTracker, get_tracker, set_tracker

__all__ = [
    "MODEL_PRICING",
    "CallCost",
    "CallRecord",
    "CostTracker",
    "get_tracker",
    "price_call",
    "resolve_model",
    "set_tracker",
]
