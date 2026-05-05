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
import time
from typing import Any, Callable, List, Optional

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.errors import AIInsufficientFunds
from bizniz.utils.json.llm import clean_llm_json


class LLMCallError(Exception):
    """The LLM call failed after all retries."""


def call_with_retry(
    *,
    client: BaseAIClient,
    messages: list,
    response_format=None,
    schema: Optional[dict] = None,
    max_attempts: int = 3,
    on_status: Optional[Callable[[str], None]] = None,
    label: str = "LLM",
) -> dict:
    """Call the LLM, retry on transient failures, parse JSON output.

    Returns the parsed JSON object. Raises ``LLMCallError`` if every
    attempt fails. Re-raises ``AIInsufficientFunds`` immediately —
    that's a billing problem, not a transient error.

    ``label`` is used in status logs so callers can identify which
    agent's call this is (e.g. "Planner", "Architect").
    """
    last_error: Any = None

    for attempt in range(1, max_attempts + 1):
        try:
            if on_status:
                on_status(f"{label}: AI call (attempt {attempt}/{max_attempts})...")
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
                continue

            text = clean_llm_json(text)
            return json.loads(text)
        except AIInsufficientFunds:
            raise
        except Exception as e:
            last_error = e
            if on_status:
                on_status(
                    f"{label}: attempt {attempt} failed — "
                    f"{type(e).__name__}: {e}"
                )
            continue

    raise LLMCallError(
        f"{label} failed after {max_attempts} attempts. "
        f"Last error: {last_error}"
    )
