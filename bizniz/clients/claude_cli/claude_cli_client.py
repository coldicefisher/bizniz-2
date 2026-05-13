"""ClaudeCliClient — BaseAIClient implementation backed by ``claude --print``.

Subprocess-shells out to the Claude Code CLI installed on the host.
Marginal cost is $0 when the user has a Max plan (subscription pays
for usage); Pro/Free users pay metered API rates. The CLI handles
auth via its own logged-in session — no API key plumbing.

Wire shape: messages → flat prompt + ``--append-system-prompt``,
subprocess captures stdout JSON, return the ``result`` field as the
response text. ``session_id`` plays the role of ``job_id`` for the
cost tracker.

What this is good for: every single-call agent (Planner, Architect,
ServicePlanner, AuthPlanner, QualityEngineer.enrich, code_examples,
CodeReviewer, etc.) — anything that hits ``BaseAIClient.get_text``
once per call.

What this is NOT: the tool-loop replacement. ``Coder`` and
``AgenticDebugger`` need a different shape (Claude's native tools +
MCP), not a JSON-schema action loop dressed up in a subprocess.
That's a separate class (``ClaudeCliCoder``), TODO.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from typing import Any, Callable, List, Optional, Tuple

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message, MessageList
from bizniz.clients.chatgpt.types.response_format import ResponseFormat


_DEFAULT_TIMEOUT_S = 600.0


class ClaudeCliClientError(Exception):
    """Subprocess-side failure (binary missing, non-zero exit, parse
    error, etc). Wraps the original error context."""


class ClaudeCliClient(BaseAIClient):
    """``BaseAIClient`` backed by the Claude Code CLI subprocess.

    Each ``get_text`` call spawns ``claude --print --output-format=json
    --append-system-prompt=<sys>`` with the user content piped via
    stdin. The CLI handles model selection, retries, and auth from
    the host's logged-in session.

    Stateless across calls — no in-process session. Message history
    is rebuilt from the caller-supplied ``message_history`` on each
    invocation. (Caching is the CLI's job; we don't try to manage it
    on this side.)
    """

    def __init__(
        self,
        model_name: str = "claude-cli",
        command: str = "claude",
        additional_args: Optional[List[str]] = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        on_message_callback: Optional[Callable[[Message], None]] = None,
    ):
        self._model_name = model_name
        self._command = command
        self._additional_args = list(additional_args or [])
        self._timeout_s = timeout_s
        self._on_message_callback = on_message_callback
        # Set by ``_client_for`` so the cost tracker can tag this
        # client's calls with the originating agent.
        self._caller_agent: str = "unknown"
        self._message_history: MessageList = []

        if shutil.which(self._command) is None:
            raise ClaudeCliClientError(
                f"Claude CLI binary {self._command!r} not on PATH. "
                f"Install Claude Code (https://docs.claude.com/en/docs/"
                f"claude-code) or set ``backends.claude_cli.command`` "
                f"in bizniz.yaml."
            )

    # ── BaseAIClient interface ──────────────────────────────────────────

    @property
    def ai_agent(self) -> Any:
        # No long-lived agent object — the subprocess IS the agent.
        return None

    def set_model(self, model_name: str) -> None:
        """Model selection on Claude CLI is at session level (the user's
        active model). We record the requested name for telemetry but
        don't override the CLI's model selection here."""
        self._model_name = model_name

    def get_text(
        self,
        messages,
        message_history: MessageList = None,
        message_history_filepath: str = None,
        use_message_history: bool = True,
        message_history_limit: int = 10,
        schema: dict = None,
        response_format: ResponseFormat = ResponseFormat.TEXT,
        max_tokens: Optional[int] = None,
        job_description: Optional[str] = None,
        temperature: float = 0.0,
        cached_content_name: Optional[str] = None,
        cache_prefix_count: int = 0,
        **kwargs,
    ) -> Tuple[str, str, List]:
        """Invoke ``claude --print`` once. Returns ``(text, session_id,
        output_messages)`` matching ``BaseAIClient.get_text``.

        ``schema``, when set with ``response_format=JSON_SCHEMA``, is
        embedded in the system prompt with a "respond with valid JSON
        matching this schema only" instruction. The CLI doesn't enforce
        the schema — that's still the caller's responsibility (same as
        for Gemini's JSON_SCHEMA path).
        """
        normalized = self._normalize_messages(messages)

        # Separate system content from user-role messages.
        system_parts: List[str] = []
        user_messages: List[dict] = []
        for msg in normalized:
            if msg["role"] == "system":
                system_parts.append(msg["content"])
            else:
                user_messages.append(msg)

        if response_format == ResponseFormat.JSON_SCHEMA and schema:
            system_parts.append(self._format_schema_prompt(schema))
        elif response_format == ResponseFormat.JSON:
            system_parts.append(
                "You must respond with valid JSON only. "
                "Do not include any prose before or after the JSON."
            )

        system_prompt = "\n\n".join(p.strip() for p in system_parts if p.strip())

        # Build the input prompt. The CLI takes a single prompt arg or
        # stdin. We concatenate user messages (role-tagged for clarity)
        # so the model sees the conversation shape. The CLI handles its
        # own caching on repeated prefixes.
        if use_message_history and self._message_history:
            history = (
                self._message_history[-message_history_limit:]
                if message_history_limit else self._message_history
            )
            all_user = history + user_messages
        else:
            all_user = user_messages
        prompt_text = self._build_prompt_text(all_user)

        cmd = [
            self._command, "--print",
            "--output-format=json",
        ]
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
        cmd.extend(self._additional_args)
        # The prompt goes via stdin — robust for arbitrary length and
        # avoids arg-list size limits.

        # Retry-with-backoff for transient server-side rate limits
        # (HTTP 429 from Anthropic's CLI backend — distinct from Max
        # plan usage caps). Schedule: 10s, 30s, 60s, then give up.
        # Total ceiling: ~100s + 3 cli round-trips. Errs on the side
        # of finishing the work over failing fast — being throttled
        # is the most common transient and the retry budget caller
        # uses (3 attempts in call_with_retry) burns through it in
        # 5 seconds without this.
        backoff_schedule = [10.0, 30.0, 60.0]
        t0 = time.time()
        proc = None
        last_429_body = ""
        for attempt_idx in range(len(backoff_schedule) + 1):
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt_text,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_s,
                )
            except subprocess.TimeoutExpired as e:
                raise ClaudeCliClientError(
                    f"claude --print timed out after {self._timeout_s:.0f}s"
                ) from e
            except FileNotFoundError as e:
                raise ClaudeCliClientError(
                    f"claude binary not found at runtime: {e}"
                ) from e

            # Detect 429 inside the JSON payload — the CLI exits 1
            # for 429s but ``proc.stdout`` is still well-formed JSON
            # with ``api_error_status: 429``. Parse pre-emptively so
            # we can backoff on this path.
            is_429 = False
            try:
                early = json.loads(proc.stdout) if proc.stdout else {}
                if (
                    early.get("api_error_status") == 429
                    or "Rate limited" in (early.get("result") or "")
                ):
                    is_429 = True
                    last_429_body = (early.get("result") or "")[:200]
            except Exception:
                pass

            if not is_429:
                break  # Either success or a non-retryable error.
            if attempt_idx >= len(backoff_schedule):
                # Exhausted retries.
                raise ClaudeCliClientError(
                    f"claude --print rate-limited (429) after "
                    f"{len(backoff_schedule) + 1} attempts: "
                    f"{last_429_body}"
                )
            wait_s = backoff_schedule[attempt_idx]
            # Best-effort status log so the user sees what's happening.
            try:
                from bizniz.cost import get_tracker as _gt
                _ = _gt()
            except Exception:
                pass
            import sys as _sys
            print(
                f"  [ClaudeCliClient] 429 rate-limit hit, "
                f"backing off {wait_s:.0f}s before retry "
                f"({attempt_idx + 1}/{len(backoff_schedule)})...",
                file=_sys.stderr, flush=True,
            )
            time.sleep(wait_s)

        if proc.returncode != 0:
            raise ClaudeCliClientError(
                f"claude --print exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[:400]}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ClaudeCliClientError(
                f"claude --print returned non-JSON: {e}\n"
                f"stdout head: {proc.stdout[:400]}"
            ) from e

        if payload.get("is_error"):
            raise ClaudeCliClientError(
                f"claude --print returned is_error=true: "
                f"{payload.get('result', '(no result)')[:400]}"
            )

        text = payload.get("result") or ""
        session_id = payload.get("session_id") or str(uuid.uuid4())
        elapsed = time.time() - t0

        # Add this turn to the local history so subsequent calls in
        # the same client instance see it. Store ONLY dicts (matching
        # the shape ``_build_prompt_text`` reads) — earlier bug:
        # mixed Message objects + dicts in the history crashed the
        # second call with ``'Message' object is not subscriptable``.
        assistant_msg = Message(role="assistant", content=text)
        self._message_history.extend(user_messages)
        self._message_history.append({"role": "assistant", "content": text})
        if self._on_message_callback:
            try:
                self._on_message_callback(assistant_msg)
            except Exception:
                pass

        output_messages: List[Message] = [assistant_msg]

        # Cost tracker hook — record token usage. Note: the tracker
        # applies API-rate pricing from its pricing table. On the Max
        # plan, the actual marginal cost reported by ``total_cost_usd``
        # is $0 (subscription absorbs it); the tracker's computed cost
        # represents "what this WOULD cost without Max" — useful as a
        # savings signal even though it's not what you paid.
        usage = payload.get("usage") or {}
        in_tokens = int(usage.get("input_tokens") or 0)
        out_tokens = int(usage.get("output_tokens") or 0)
        cached_in = (
            int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
        )
        try:
            from bizniz.cost import get_tracker
            tracker = get_tracker()
            if tracker is not None:
                tracker.record(
                    agent=self._caller_agent,
                    model=self._model_name,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    duration_ms=int(elapsed * 1000),
                    cached_input_tokens=cached_in,
                )
        except Exception:
            # Cost tracker is optional; never fail a call because of it.
            pass

        return text, session_id, output_messages

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_messages(messages) -> List[dict]:
        """Accept the same shapes the other clients do: a string, a
        single Message, a list of Messages, or a list of dicts."""
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        if isinstance(messages, Message):
            return [{"role": messages.role, "content": messages.content}]
        out: List[dict] = []
        for m in messages or []:
            if isinstance(m, Message):
                out.append({"role": m.role, "content": m.content})
            elif isinstance(m, dict) and "role" in m and "content" in m:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    @staticmethod
    def _build_prompt_text(user_messages: List[dict]) -> str:
        """Concatenate role-tagged user/assistant messages into a single
        prompt string. The CLI accepts free text; tagging makes the
        prior turns legible to the model when message_history is on."""
        if not user_messages:
            return ""
        if len(user_messages) == 1 and user_messages[0]["role"] == "user":
            return user_messages[0]["content"]
        parts: List[str] = []
        for m in user_messages:
            role = m["role"].upper()
            parts.append(f"[{role}]\n{m['content']}")
        return "\n\n".join(parts)

    @staticmethod
    def _format_schema_prompt(schema: dict) -> str:
        """JSON_SCHEMA mode: tell the model to emit JSON matching the
        schema. Same shape as the Gemini client's helper.
        """
        try:
            schema_str = json.dumps(schema, indent=2)
        except Exception:
            schema_str = str(schema)
        return (
            "Respond with a SINGLE JSON object matching exactly this "
            "schema. No prose before or after. No markdown fences.\n\n"
            f"Schema:\n{schema_str}"
        )
