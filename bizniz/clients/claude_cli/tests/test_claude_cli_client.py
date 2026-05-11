"""Tests for ClaudeCliClient.

Live CLI calls are deferred to ``@pytest.mark.functional``. These
tests mock subprocess and verify the command shape, prompt
assembly, and JSON handling.
"""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.clients.claude_cli.claude_cli_client import (
    ClaudeCliClient,
    ClaudeCliClientError,
)


def _fake_proc(text: str = "hi", session_id: str = "sid-1", returncode: int = 0):
    """Minimal stub of the CLI's --output-format=json payload."""
    payload = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": session_id,
        "total_cost_usd": 0.0,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 3,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    })
    p = MagicMock()
    p.stdout = payload
    p.stderr = ""
    p.returncode = returncode
    return p


class TestClientShape:
    def test_init_raises_when_binary_missing(self):
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.shutil.which",
            return_value=None,
        ):
            with pytest.raises(ClaudeCliClientError) as exc:
                ClaudeCliClient()
            assert "not on PATH" in str(exc.value)

    def test_init_ok_when_binary_on_path(self):
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.shutil.which",
            return_value="/usr/bin/claude",
        ):
            c = ClaudeCliClient()
            assert c._command == "claude"

    def test_ai_agent_returns_none(self):
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.shutil.which",
            return_value="/usr/bin/claude",
        ):
            assert ClaudeCliClient().ai_agent is None


class TestGetText:
    def _client(self):
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.shutil.which",
            return_value="/usr/bin/claude",
        ):
            return ClaudeCliClient()

    def test_returns_result_text_and_session_id(self):
        c = self._client()
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=_fake_proc(text="four", session_id="abc-123"),
        ):
            text, sid, msgs = c.get_text(
                messages="What is 2+2?",
                use_message_history=False,
            )
        assert text == "four"
        assert sid == "abc-123"
        assert len(msgs) == 1
        assert msgs[0].role == "assistant"
        assert msgs[0].content == "four"

    def test_system_prompt_via_flag(self):
        c = self._client()
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=_fake_proc(),
        ) as m:
            c.get_text(
                messages=[
                    Message(role="system", content="You are terse."),
                    Message(role="user", content="hi"),
                ],
                use_message_history=False,
            )
        argv = m.call_args.args[0]
        # --append-system-prompt should be present with the system content
        idx = argv.index("--append-system-prompt")
        assert "You are terse." in argv[idx + 1]

    def test_user_content_via_stdin(self):
        c = self._client()
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=_fake_proc(),
        ) as m:
            c.get_text(messages="say hi please", use_message_history=False)
        # stdin is passed via the ``input`` kwarg, not as a CLI arg
        assert m.call_args.kwargs["input"] == "say hi please"

    def test_json_schema_mode_appends_schema_to_system(self):
        c = self._client()
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=_fake_proc(text='{"x": 1}'),
        ) as m:
            c.get_text(
                messages="give me x=1",
                schema=schema,
                response_format=ResponseFormat.JSON_SCHEMA,
                use_message_history=False,
            )
        argv = m.call_args.args[0]
        idx = argv.index("--append-system-prompt")
        sys_prompt = argv[idx + 1]
        assert "JSON" in sys_prompt
        assert "Schema:" in sys_prompt
        assert "integer" in sys_prompt

    def test_raises_on_non_zero_exit(self):
        c = self._client()
        bad = _fake_proc(returncode=2)
        bad.stderr = "boom"
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=bad,
        ):
            with pytest.raises(ClaudeCliClientError) as exc:
                c.get_text(messages="x", use_message_history=False)
        assert "exited 2" in str(exc.value)

    def test_raises_on_is_error_true(self):
        c = self._client()
        payload = json.dumps({
            "type": "result", "is_error": True,
            "result": "Not logged in", "session_id": "sid",
        })
        proc = MagicMock(stdout=payload, stderr="", returncode=0)
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=proc,
        ):
            with pytest.raises(ClaudeCliClientError) as exc:
                c.get_text(messages="x", use_message_history=False)
        assert "is_error=true" in str(exc.value)

    def test_raises_on_timeout(self):
        c = self._client()
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            side_effect=subprocess.TimeoutExpired("claude", 5),
        ):
            with pytest.raises(ClaudeCliClientError) as exc:
                c.get_text(messages="x", use_message_history=False)
        assert "timed out" in str(exc.value)


class TestHistory:
    def _client(self):
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.shutil.which",
            return_value="/usr/bin/claude",
        ):
            return ClaudeCliClient()

    def test_history_round_trip_doesnt_crash_on_second_call(self):
        """Regression: ``_message_history`` used to mix Message and
        dict entries, crashing ``_build_prompt_text`` on the second
        call with ``'Message' object is not subscriptable``."""
        c = self._client()
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=_fake_proc(text="A1"),
        ):
            c.get_text(messages="first", use_message_history=True)

        # Second call with history must succeed and see prior context.
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=_fake_proc(text="A2"),
        ) as m:
            c.get_text(messages="second", use_message_history=True)
        prompt = m.call_args.kwargs["input"]
        # Both prior user msg, prior assistant, and current user
        # should appear in the assembled prompt.
        assert "first" in prompt
        assert "A1" in prompt
        assert "second" in prompt


class TestPromptAssembly:
    def test_single_user_is_bare(self):
        out = ClaudeCliClient._build_prompt_text([
            {"role": "user", "content": "hello"},
        ])
        assert out == "hello"

    def test_multi_message_tagged(self):
        out = ClaudeCliClient._build_prompt_text([
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
        ])
        assert "[USER]\nfirst" in out
        assert "[ASSISTANT]\nok" in out
        assert "[USER]\nsecond" in out

    def test_normalize_accepts_string(self):
        out = ClaudeCliClient._normalize_messages("hello")
        assert out == [{"role": "user", "content": "hello"}]

    def test_normalize_accepts_message(self):
        out = ClaudeCliClient._normalize_messages(
            Message(role="system", content="be terse"),
        )
        assert out == [{"role": "system", "content": "be terse"}]

    def test_normalize_accepts_dict_list(self):
        out = ClaudeCliClient._normalize_messages([
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ])
        assert len(out) == 2
        assert out[1]["content"] == "y"
