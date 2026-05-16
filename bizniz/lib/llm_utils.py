"""Shared LLM helpers for v2 single-call agents.

Single-call agents (Planner, Architect, TestReviewer) all want the same
two things: retry on transient AI errors, and parse JSON output back
into a typed dict. They don't share enough else to justify a base
class — these helpers are the entire shared surface.

Tool-loop agents (ServiceImplementer, IntegrationDebugger) inherit
``ToolLoopAgent`` and don't use these helpers — the loop has its own
retry / dispatch infrastructure.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional, Tuple

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.errors import AIInsufficientFunds
from bizniz.utils.json.llm import clean_llm_json


class LLMCallError(Exception):
    """The LLM call failed after all retries."""


def _resolve_transient_backoff() -> Tuple[float, ...]:
    """Default transient backoff schedule. Override via env var
    ``BIZNIZ_TRANSIENT_BACKOFF_S`` (comma-separated seconds, e.g.
    ``"30,90,300,600"``) when you need to tune it without changing
    code. The default absorbs a typical Anthropic 5xx outage
    (~5-15 min) early and stretches to ~1.7h total wait before
    surfacing — long enough that a 5-hour build survives even an
    unusually long upstream incident."""
    raw = os.environ.get("BIZNIZ_TRANSIENT_BACKOFF_S")
    if not raw:
        return (30.0, 90.0, 300.0, 600.0, 1800.0, 3600.0)
    out: list[float] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(float(piece))
        except ValueError:
            continue
    return tuple(out) if out else (30.0, 90.0, 300.0, 600.0, 1800.0, 3600.0)


# Substrings (case-insensitive) that indicate a transient upstream
# infrastructure failure — Anthropic/OpenAI 5xx, network blip, etc.
# These get a longer retry budget with exponential backoff rather
# than the fast 3-shot retry we apply to permanent errors (e.g. the
# LLM emitting bad JSON, schema violations).
_TRANSIENT_PATTERNS: Tuple[str, ...] = (
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "gateway time-out",
    "api error: 500",
    "api error: 502",
    "api error: 503",
    "api error: 504",
    '"api_error_status":500',
    '"api_error_status":502',
    '"api_error_status":503',
    '"api_error_status":504',
    '"api_error_status": 500',
    '"api_error_status": 502',
    '"api_error_status": 503',
    '"api_error_status": 504',
    "connection reset",
    "connection refused",
    "connection aborted",
    "timed out",
    "read timeout",
    "remote disconnected",
    "remote end closed",
)

# Backoff schedule for transient errors. Each entry is the wait BEFORE
# the next retry attempt; cumulative budget is ~1.7 hours so the build
# survives unusually long Anthropic outages instead of halting a
# 5-hour build on a 10-minute upstream incident. This mirrors the
# Max-plan usage-cap wait philosophy already in ClaudeCliClient: when
# the upstream is the problem, wait it out in-process; the caller's
# job is unchanged when the API recovers.
_DEFAULT_TRANSIENT_BACKOFF_S: Tuple[float, ...] = _resolve_transient_backoff()


def _is_transient(exc: BaseException) -> bool:
    """Return True if the exception text looks like a transient upstream
    infrastructure failure rather than a permanent bad-input/bad-output
    issue."""
    text = str(exc).lower()
    return any(p in text for p in _TRANSIENT_PATTERNS)


def call_with_retry(
    *,
    client: BaseAIClient,
    messages: list,
    response_format=None,
    schema: Optional[dict] = None,
    max_attempts: int = 3,
    max_transient_attempts: int = 7,
    transient_backoff_s: Tuple[float, ...] = _DEFAULT_TRANSIENT_BACKOFF_S,
    on_status: Optional[Callable[[str], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "LLM",
) -> dict:
    """Call the LLM, retry on transient failures, parse JSON output.

    Two separate retry budgets:

    - ``max_attempts`` — permanent-failure budget (default 3). Bad JSON,
      schema violations, empty responses. No backoff between attempts
      since the LLM rerolling instantly is fine.
    - ``max_transient_attempts`` — transient infrastructure budget
      (default 7). Anthropic 5xx, network blips, gateway timeouts.
      Backoff per ``transient_backoff_s`` between attempts (default
      30/90/300/600/1800/3600s, ~1.7h cumulative wait — wait the
      outage out in-process, just like ClaudeCliClient already does
      for Max-plan usage caps). Override via env var
      ``BIZNIZ_TRANSIENT_BACKOFF_S=30,90,300,600`` if you want shorter
      waits during testing.

    Returns the parsed JSON object. Raises ``LLMCallError`` if either
    budget is exhausted. Re-raises ``AIInsufficientFunds`` immediately —
    that's a billing problem, not a transient error.

    ``label`` is used in status logs so callers can identify which
    agent's call this is (e.g. "Planner", "Architect"). ``sleep`` is
    injectable for tests.
    """
    last_error: Any = None
    transient_count = 0
    permanent_count = 0

    while True:
        attempt = transient_count + permanent_count + 1
        try:
            if on_status:
                on_status(
                    f"{label}: AI call (attempt {attempt}/"
                    f"{max_attempts + max_transient_attempts})..."
                )
            t0 = time.time()
            kwargs = {"messages": messages}
            if response_format is not None:
                kwargs["response_format"] = response_format
            if schema is not None:
                kwargs["schema"] = schema
            text, _job_id, _output_messages = client.get_text(**kwargs)
            elapsed = time.time() - t0
            if on_status:
                on_status(
                    f"{label}: AI responded in {elapsed:.1f}s "
                    f"({len(text or '')} chars)"
                )

            if not text or not text.strip():
                last_error = "Empty response from AI"
                permanent_count += 1
                if permanent_count >= max_attempts:
                    break
                continue

            text = clean_llm_json(text)
            return json.loads(text)
        except AIInsufficientFunds:
            raise
        except Exception as e:
            last_error = e
            if _is_transient(e):
                transient_count += 1
                if transient_count >= max_transient_attempts:
                    if on_status:
                        on_status(
                            f"{label}: attempt {attempt} transient — "
                            f"{type(e).__name__}: {e}"
                        )
                    break
                idx = min(transient_count - 1, len(transient_backoff_s) - 1)
                wait_s = transient_backoff_s[idx]
                if on_status:
                    on_status(
                        f"{label}: attempt {attempt} transient (upstream "
                        f"infrastructure) — backing off {wait_s:.0f}s before "
                        f"retry ({transient_count}/{max_transient_attempts})"
                    )
                sleep(wait_s)
                continue
            else:
                permanent_count += 1
                if on_status:
                    on_status(
                        f"{label}: attempt {attempt} failed — "
                        f"{type(e).__name__}: {e}"
                    )
                if permanent_count >= max_attempts:
                    break
                continue

    raise LLMCallError(
        f"{label} failed after {transient_count + permanent_count} "
        f"attempts ({transient_count} transient, {permanent_count} "
        f"permanent). Last error: {last_error}"
    )
