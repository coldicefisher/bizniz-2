"""Tests for ToolLoopAgent ABC.

We define a minimal concrete subclass (``EchoAgent``) here in the test
file so we can exercise the loop infrastructure without depending on
a real agent implementation.
"""
import json
from unittest.mock import MagicMock

import pytest

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.lib.tool_loop_agent import (
    ToolLoopAgent,
    ToolLoopAgentBadResponseError,
    ToolLoopAgentNoTerminalError,
)
from bizniz.workspace.base_workspace import BaseWorkspace


# ── A minimal concrete subclass for testing the ABC ──────────────────────────

_ECHO_SCHEMA = {
    "name": "echo_action",
    "strict": True,
    "schema": {
        "type": "object",
        "required": ["action", "value"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["echo", "uppercase", "submit_result"],
            },
            "value": {"type": "string"},
        },
        "additionalProperties": False,
    },
}


class EchoAgent(ToolLoopAgent):
    """Test agent: ``echo`` and ``uppercase`` are tool actions,
    ``submit_result`` is the terminal action that returns the value."""

    @property
    def system_prompt(self) -> str:
        return "Test agent."

    @property
    def action_schema(self) -> dict:
        return _ECHO_SCHEMA

    @property
    def terminal_action(self) -> str:
        return "submit_result"

    def tool_handlers(self):
        return {
            "echo": lambda a: f"echo: {a.get('value', '')}",
            "uppercase": lambda a: a.get("value", "").upper(),
        }

    def parse_terminal_action(self, action: dict):
        return action.get("value", "")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resp(action: dict):
    return json.dumps(action), "job-id", []


def _make_agent(client_responses, tool_iterations=10, timeout_seconds=60):
    client = MagicMock(spec=BaseAIClient)
    client.get_text.side_effect = client_responses
    workspace = MagicMock(spec=BaseWorkspace)
    return EchoAgent(
        client=client,
        workspace=workspace,
        tool_iterations=tool_iterations,
        timeout_seconds=timeout_seconds,
    ), client


# ── Terminal action ──────────────────────────────────────────────────────────

class TestTerminal:
    def test_terminal_action_returns_parsed_payload(self):
        agent, client = _make_agent([
            _resp({"action": "submit_result", "value": "hello"}),
        ])
        result = agent.run("start")
        assert result == "hello"
        assert client.get_text.call_count == 1

    def test_terminal_action_after_some_tool_calls(self):
        agent, client = _make_agent([
            _resp({"action": "echo", "value": "first"}),
            _resp({"action": "uppercase", "value": "second"}),
            _resp({"action": "submit_result", "value": "done"}),
        ])
        result = agent.run("start")
        assert result == "done"
        assert client.get_text.call_count == 3


# ── Tool dispatch ────────────────────────────────────────────────────────────

class TestToolDispatch:
    def test_unknown_action_logs_and_continues(self):
        """An unknown action gets a corrective message, agent retries
        and ultimately submits."""
        agent, client = _make_agent([
            _resp({"action": "nonsense", "value": "x"}),
            _resp({"action": "submit_result", "value": "done"}),
        ])
        result = agent.run("start")
        assert result == "done"
        assert client.get_text.call_count == 2

    def test_handler_exception_is_reported_to_agent(self):
        """If a handler raises, the loop survives and feeds the error
        back into the conversation. The agent can recover."""
        responses = [
            _resp({"action": "echo", "value": "boom"}),
            _resp({"action": "submit_result", "value": "recovered"}),
        ]
        client = MagicMock(spec=BaseAIClient)
        client.get_text.side_effect = responses

        class BoomAgent(EchoAgent):
            def tool_handlers(self):
                return {
                    "echo": self._raise,
                    "uppercase": lambda a: "fine",
                }

            def _raise(self, action):
                raise RuntimeError("kaboom")

        agent = BoomAgent(
            client=client,
            workspace=MagicMock(spec=BaseWorkspace),
        )
        result = agent.run("start")
        assert result == "recovered"


# ── Parse failures ───────────────────────────────────────────────────────────

class TestParseFailures:
    def test_malformed_json_recovers_within_threshold(self):
        agent, client = _make_agent([
            ("not json at all", "job-id", []),
            ("still not json", "job-id", []),
            _resp({"action": "submit_result", "value": "ok"}),
        ])
        result = agent.run("start")
        assert result == "ok"

    def test_repeated_parse_failure_raises(self):
        agent, client = _make_agent([
            ("bad", "job-id", []),
            ("worse", "job-id", []),
            ("worst", "job-id", []),
        ])
        with pytest.raises(ToolLoopAgentBadResponseError):
            agent.run("start")

    def test_empty_response_counts_as_failure(self):
        agent, client = _make_agent([
            ("", "job-id", []),
            ("", "job-id", []),
            ("", "job-id", []),
        ])
        with pytest.raises(ToolLoopAgentBadResponseError):
            agent.run("start")


# ── Iteration cap ────────────────────────────────────────────────────────────

class TestIterationCap:
    def test_force_terminal_after_cap(self):
        """When the agent uses up all iterations, the loop forces a
        final 'you MUST submit' call."""
        # 3 iterations of echo, then forced-final returns terminal
        responses = [
            _resp({"action": "echo", "value": "loop1"}),
            _resp({"action": "echo", "value": "loop2"}),
            _resp({"action": "echo", "value": "loop3"}),
            # forced-final call returns terminal
            _resp({"action": "submit_result", "value": "forced"}),
        ]
        agent, client = _make_agent(responses, tool_iterations=3)
        result = agent.run("start")
        assert result == "forced"
        assert client.get_text.call_count == 4

    def test_force_terminal_still_non_terminal_raises(self):
        """If the forced-final call STILL doesn't return the terminal
        action, raise NoTerminalError."""
        responses = [
            _resp({"action": "echo", "value": "loop1"}),
            # forced-final still echoes
            _resp({"action": "echo", "value": "still echoing"}),
        ]
        agent, client = _make_agent(responses, tool_iterations=1)
        with pytest.raises(ToolLoopAgentNoTerminalError):
            agent.run("start")
