"""``ToolLoopAgent`` — base class for v2 tool-using agents.

Three concrete subclasses are planned: ``AuthAgent``, ``ServiceImplementer``,
``IntegrationDebugger``. Single-call agents (Planner, Architect,
TestReviewer) do NOT inherit from this — they use plain functions in
``bizniz.lib.llm_utils`` instead.

The ABC owns:
  - The tool-using conversation loop
  - LLM-call retries on parse / network failures
  - Action parsing + dispatch (tool actions vs the terminal action)
  - Per-iteration timeout, total-time timeout, max-iterations cap
  - Forced-final-call when iteration cap is hit but no terminal action emitted

Subclasses provide:
  - ``system_prompt`` — what the agent's role is
  - ``action_schema`` — the JSON schema for the agent's actions (the
    enum of action types the agent can call, including its terminal
    action)
  - ``terminal_action`` — the action name that ends the loop, e.g.
    ``"submit_fix"``, ``"submit_contract"``
  - ``tool_handlers`` — dict mapping action_type → callable that takes
    the action dict and returns a string result (fed back into the
    conversation as the next user message)
  - ``parse_terminal_action`` — turn the terminal action's payload into
    the typed result the subclass's public method returns
  - A typed entry method (e.g. ``configure()``, ``implement()``,
    ``debug()``) that builds an initial context string and calls
    ``self.run(initial_context)``

Universal tool plumbing (``view_file``, ``run_in_container``,
``hit_endpoint``, etc.) lives in ``bizniz.lib.tools`` (forthcoming);
each subclass picks the subset it wires into its ``tool_handlers``.
For now subclasses build their tool_handlers dict directly.
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.clients.errors import AIInsufficientFunds
from bizniz.utils.json.llm import clean_llm_json
from bizniz.workspace.base_workspace import BaseWorkspace


class ToolLoopAgentError(Exception):
    """Base class for ToolLoopAgent failures."""


class ToolLoopAgentTimeoutError(ToolLoopAgentError):
    """Wall-clock timeout exceeded before the agent emitted a terminal action."""


class ToolLoopAgentBadResponseError(ToolLoopAgentError):
    """LLM repeatedly produced unparseable / failed responses."""


class ToolLoopAgentNoTerminalError(ToolLoopAgentError):
    """The agent hit its iteration cap and even the forced-final call did
    not return a terminal action."""


class TerminalActionRejected(Exception):
    """Subclass raised this from ``parse_terminal_action`` to push the
    LLM back into the loop with a correction message. Loop appends
    ``reason`` as a user message and continues.

    Used to gate self-reported terminal status: e.g. Coder claims
    ``status="passed"`` but tests aren't actually green.
    """
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ToolLoopAgentStalledError(ToolLoopAgentError):
    """The agent emitted the same action signature too many times in
    its recent window (default: 3 of the last 5). Caller should
    escalate to a higher-tier model rather than continue spinning.

    ``last_action`` carries the repeated action's signature so the
    caller can include it in escalation logs."""
    def __init__(self, message: str, last_action: str = ""):
        super().__init__(message)
        self.last_action = last_action


# A tool handler takes the parsed action dict and returns the result
# string that will be appended to the conversation as the next user
# message. Handlers are responsible for their own error formatting —
# return an "ERROR: <reason>" string rather than raising.
ToolHandler = Callable[[Dict[str, Any]], str]


class ToolLoopAgent(ABC):
    """ABC for v2 tool-using agents (AuthAgent, ServiceImplementer,
    IntegrationDebugger).

    Parameters
    ----------
    client:
        The LLM client. Each agent should hold a dedicated instance to
        avoid message-history contamination across agents.
    workspace:
        The workspace this agent operates on. Available to subclass tool
        handlers as ``self._workspace``.
    on_status:
        Optional logging callback. The ABC logs key transitions
        (turn N, action X, terminal submitted, timeout, etc.).
    tool_iterations:
        Hard cap on the number of LLM round-trips per ``run()`` call.
        On exhaustion the loop forces a final "you MUST submit now" call.
    timeout_seconds:
        Wall-clock cap. Same forced-final behavior as iteration cap.
    """

    def __init__(
        self,
        client: BaseAIClient,
        workspace: BaseWorkspace,
        on_status: Optional[Callable[[str], None]] = None,
        tool_iterations: int = 40,
        timeout_seconds: int = 600,
        history_window: int = 0,
        stall_window: int = 5,
        stall_threshold: int = 3,
    ):
        """``history_window``: when > 0, sliding-window compaction kicks in.
        Default 0 = full history (smoke runs showed compaction made the
        Engineer churn more iterations than it saved cost).

        ``stall_window`` / ``stall_threshold`` (default 5/3): if the
        agent emits the same action signature ``stall_threshold`` times
        within the last ``stall_window`` iterations, raise
        ``ToolLoopAgentStalledError`` so the caller can escalate to a
        higher-tier model rather than burn iterations on a stuck loop.
        """
        self._client = client
        self._workspace = workspace
        self._on_status = on_status
        self._tool_iterations = tool_iterations
        self._timeout_seconds = timeout_seconds
        self._history_window = max(0, history_window)
        self._stall_window = max(2, stall_window)
        self._stall_threshold = max(2, stall_threshold)

    # ── Subclass contract ────────────────────────────────────────────────────

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """The agent's role + tool descriptions + workflow rules."""

    @property
    @abstractmethod
    def action_schema(self) -> dict:
        """JSON schema for one agent action. The schema's ``action`` enum
        must include every key in ``tool_handlers`` AND ``terminal_action``."""

    @property
    @abstractmethod
    def terminal_action(self) -> str:
        """Action name that ends the loop (e.g. ``"submit_fix"``)."""

    @abstractmethod
    def tool_handlers(self) -> Dict[str, ToolHandler]:
        """Map of action name → handler. Handlers receive the parsed action
        dict and return a string result. Built per-instance because handlers
        usually close over instance state (workspace, compose path, etc.)."""

    @abstractmethod
    def parse_terminal_action(self, action: dict) -> Any:
        """Turn the terminal action's payload into the typed result the
        subclass's public method returns."""

    # ── The loop ────────────────────────────────────────────────────────────

    def run(self, initial_user_message: str) -> Any:
        """Run the tool-loop until the agent emits its terminal action.

        Returns whatever ``parse_terminal_action`` produces.
        """
        agent_name = type(self).__name__
        self._log(f"{agent_name}: starting...")

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": initial_user_message},
        ]

        handlers = self.tool_handlers()
        terminal = self.terminal_action

        # PROMPT CACHING (lever A): opt-in via ``self._enable_prompt_cache``
        # (default False). The combination of cached_content + JSON_SCHEMA
        # response_format + sliding-window history caused empty-action
        # responses in initial smoke runs — needs more investigation.
        # Sliding-window (B) and pre-rendered context (E) work
        # independently of caching; they bring most of the cost win
        # without the integration risk.
        cache_name: Optional[str] = None
        cache_prefix_count = 0
        if getattr(self, "_enable_prompt_cache", False):
            try:
                cache_name = self._client.try_create_cache(messages)
                if cache_name:
                    cache_prefix_count = 2
                    self._log(
                        f"{agent_name}: prompt cache created "
                        f"(prefix=2 messages) — {cache_name}"
                    )
            except Exception as e:
                self._log(
                    f"{agent_name}: try_create_cache raised "
                    f"{type(e).__name__}: {e}"
                )

        start_time = time.time()
        parse_failures = 0
        max_parse_failures = 3

        # Stall detection: deque of recent action signatures. If any
        # signature appears ``stall_threshold`` times in the last
        # ``stall_window`` actions, raise ToolLoopAgentStalledError so
        # the caller can escalate models rather than burn iterations.
        from collections import deque
        recent_actions: deque = deque(maxlen=self._stall_window)

        for turn in range(1, self._tool_iterations + 1):
            elapsed = time.time() - start_time
            if elapsed > self._timeout_seconds:
                self._log(
                    f"{agent_name}: timeout after {int(elapsed)}s — "
                    f"forcing terminal action"
                )
                return self._force_terminal(messages, agent_name, reason="timeout")

            messages = self._compact_history(messages, agent_name)
            text = self._call_llm(
                messages, agent_name,
                cached_content_name=cache_name,
                cache_prefix_count=cache_prefix_count,
            )
            if text is None:
                parse_failures += 1
                if parse_failures >= max_parse_failures:
                    raise ToolLoopAgentBadResponseError(
                        f"{agent_name}: LLM call failed {max_parse_failures} times"
                    )
                continue

            try:
                action = json.loads(clean_llm_json(text))
            except Exception as e:
                parse_failures += 1
                self._log(f"{agent_name}: failed to parse response ({e})")
                if parse_failures >= max_parse_failures:
                    raise ToolLoopAgentBadResponseError(
                        f"{agent_name}: failed to parse LLM response "
                        f"{max_parse_failures} times"
                    )
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": "Your response was not valid JSON. Please try again.",
                })
                continue

            messages.append({"role": "assistant", "content": text})

            action_type = action.get("action", "")
            if action_type == terminal:
                self._log(f"{agent_name}: terminal action submitted")
                try:
                    return self.parse_terminal_action(action)
                except TerminalActionRejected as e:
                    self._log(
                        f"{agent_name}: terminal rejected — {e.reason[:160]}"
                    )
                    messages.append({"role": "user", "content": e.reason})
                    continue

            handler = handlers.get(action_type)
            if handler is None:
                self._log(f"{agent_name}: unknown action '{action_type}'")
                messages.append({
                    "role": "user",
                    "content": (
                        f"Unknown action '{action_type}'. Available actions: "
                        f"{', '.join(sorted(handlers.keys()) + [terminal])}."
                    ),
                })
                continue

            self._log_action(agent_name, action_type, action)

            # Stall detection. Signature is the action dict minus
            # ``thinking`` (free-text reasoning that varies legitimately
            # call-to-call). All other fields participate so identical
            # actions with different payloads don't collide.
            try:
                sig = json.dumps(
                    {k: v for k, v in action.items() if k != "thinking"},
                    sort_keys=True, default=str,
                )
            except Exception:
                sig = action_type
            recent_actions.append(sig)
            sig_count = sum(1 for s in recent_actions if s == sig)
            if sig_count >= self._stall_threshold:
                self._log(
                    f"{agent_name}: STALL — same action {sig_count}x in "
                    f"last {len(recent_actions)} iterations: {sig}"
                )
                raise ToolLoopAgentStalledError(
                    f"{agent_name}: stalled — same action repeated "
                    f"{sig_count}x in last {len(recent_actions)} iterations",
                    last_action=str(sig),
                )

            try:
                result = handler(action)
            except Exception as e:
                result = f"ERROR: tool '{action_type}' raised {type(e).__name__}: {e}"
                self._log(f"{agent_name}: tool '{action_type}' raised — {e}")

            messages.append({
                "role": "user",
                "content": f"[TOOL RESULT: {action_type}]\n{result}",
            })

        # Iteration cap exhausted — force a final terminal call.
        self._log(
            f"{agent_name}: iteration cap reached ({self._tool_iterations}) — "
            f"forcing terminal action"
        )
        return self._force_terminal(messages, agent_name, reason="iteration_cap")

    # ── Internals ────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    def _compact_history(
        self, messages: List[Dict[str, str]], agent_name: str,
    ) -> List[Dict[str, str]]:
        """Sliding-window compaction. Keep system + initial_user (the
        first 2 messages — they're always preserved) and the most recent
        ``history_window`` assistant/user pairs. Drop everything in
        between.

        Disabled when ``history_window <= 0``.
        """
        if self._history_window <= 0:
            return messages
        # 2 anchor messages + 2 messages per pair = anchor + window*2.
        max_total = 2 + self._history_window * 2
        if len(messages) <= max_total:
            return messages
        kept = messages[:2] + messages[-(self._history_window * 2):]
        dropped = len(messages) - len(kept)
        if dropped > 0:
            # Insert a brief synthetic note so the LLM knows context was
            # truncated and it can re-read files/get_my_plan if needed.
            kept = (
                kept[:2]
                + [{
                    "role": "user",
                    "content": (
                        f"(System: {dropped} earlier message(s) were "
                        f"compacted to keep cost bounded. If you need "
                        f"older context, use view_file / get_my_plan / "
                        f"discovery tools to re-fetch.)"
                    ),
                }]
                + kept[2:]
            )
            self._log(
                f"{agent_name}: compacted history ({len(messages)}→{len(kept)} messages)"
            )
        return kept

    def _log_action(self, agent_name: str, action_type: str, action: dict) -> None:
        """One-line action log. Subclasses can override for richer logging.

        Default formatting: include path / service / command / url if
        present, since those are the most informative bits at a glance.
        """
        bits = []
        for key in ("service", "path", "url", "command"):
            v = action.get(key)
            if v:
                v_str = str(v)
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
                bits.append(f"{key}={v_str!r}")
        suffix = f" ({', '.join(bits)})" if bits else ""
        self._log(f"{agent_name}: {action_type}{suffix}")

    def _call_llm(
        self,
        messages: List[Dict[str, str]],
        agent_name: str,
        cached_content_name: Optional[str] = None,
        cache_prefix_count: int = 0,
    ) -> Optional[str]:
        """Call the LLM. Returns the raw text on success, None on
        retryable failure (caller will increment parse_failures).
        Re-raises ``AIInsufficientFunds`` immediately."""
        try:
            text, _job_id, _output_messages = self._client.get_text(
                messages=messages,
                use_message_history=False,
                response_format=ResponseFormat.JSON_SCHEMA,
                schema=self.action_schema,
                cached_content_name=cached_content_name,
                cache_prefix_count=cache_prefix_count,
            )
        except AIInsufficientFunds:
            raise
        except Exception as e:
            self._log(f"{agent_name}: LLM call failed ({type(e).__name__}: {e})")
            return None

        if not text or not text.strip():
            self._log(f"{agent_name}: LLM returned empty response")
            return None
        return text

    def _force_terminal(
        self,
        messages: List[Dict[str, str]],
        agent_name: str,
        reason: str,
    ) -> Any:
        """Append a 'you MUST submit now' nudge and make one final LLM
        call. If even that doesn't produce the terminal action, raise
        ``ToolLoopAgentNoTerminalError`` with the conversation tail
        for caller diagnostics."""
        messages.append({
            "role": "user",
            "content": (
                f"You have reached the {reason}. You MUST submit "
                f"action='{self.terminal_action}' NOW with your best result "
                f"based on what you've learned so far. Any other action will "
                f"be rejected."
            ),
        })

        text = self._call_llm(messages, agent_name)
        if text is None:
            raise ToolLoopAgentNoTerminalError(
                f"{agent_name}: forced-final LLM call returned no text"
            )

        try:
            action = json.loads(clean_llm_json(text))
        except Exception as e:
            raise ToolLoopAgentNoTerminalError(
                f"{agent_name}: forced-final response unparseable ({e})"
            )

        if action.get("action") != self.terminal_action:
            raise ToolLoopAgentNoTerminalError(
                f"{agent_name}: forced-final still returned non-terminal "
                f"action '{action.get('action')}' instead of "
                f"'{self.terminal_action}'"
            )

        self._log(f"{agent_name}: terminal action submitted (forced)")
        return self.parse_terminal_action(action)
