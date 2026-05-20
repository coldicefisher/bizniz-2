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

    def test_tool_use_disabled(self):
        """Regression for recipe_box: WebUITester emitted a 700-byte
        narrative ("Wrote 9 Playwright tests...") because Claude
        treated the prompt as a Write-tool task. The basic client
        is text-only — must explicitly disable the writing tools.

        Note: ``--allowed-tools ""`` is interpreted as "use defaults"
        by the CLI; the flag that actually works is ``--disallowedTools``
        with explicit tool names."""
        c = self._client()
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=_fake_proc(),
        ) as m:
            c.get_text(messages="hi", use_message_history=False)
        argv = m.call_args.args[0]
        idx = argv.index("--disallowedTools")
        disallowed = argv[idx + 1]
        for tool in ("Edit", "Write", "Bash"):
            assert tool in disallowed, f"{tool} must be in --disallowedTools"

    def test_resume_session_id_adds_resume_flag(self):
        """v4 Option 2 (2026-05-19): when ``resume_session_id`` is
        passed, the CLI command includes ``--resume <id>`` so the
        prompt cache stays warm across fix-passes."""
        c = self._client()
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=_fake_proc(),
        ) as m:
            c.get_text(
                messages="continuation",
                use_message_history=False,
                resume_session_id="prior-sess-abc",
            )
        argv = m.call_args.args[0]
        assert "--resume" in argv
        idx = argv.index("--resume")
        assert argv[idx + 1] == "prior-sess-abc"

    def test_no_resume_flag_when_session_id_none(self):
        """Default path: no ``--resume`` flag → fresh session."""
        c = self._client()
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=_fake_proc(),
        ) as m:
            c.get_text(messages="hi", use_message_history=False)
        argv = m.call_args.args[0]
        assert "--resume" not in argv

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


def _fake_429_proc():
    """Stub a 429 response. The CLI exits 1 but stdout has structured
    JSON with ``api_error_status: 429`` and ``result`` containing a
    'Rate limited' message — same shape we saw on recipe_box."""
    payload = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": 429,
        "result": "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited",
        "session_id": "sid-429",
        "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    })
    p = MagicMock()
    p.stdout = payload
    p.stderr = ""
    p.returncode = 1
    return p


class TestRateLimitRetry:
    def _client(self):
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.shutil.which",
            return_value="/usr/bin/claude",
        ):
            return ClaudeCliClient()

    def test_retries_429_until_success(self):
        """Two 429s, then success — should return successfully."""
        c = self._client()
        responses = [_fake_429_proc(), _fake_429_proc(), _fake_proc("ok")]
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            side_effect=responses,
        ), patch(
            "bizniz.clients.claude_cli.claude_cli_client.time.sleep"
        ) as mock_sleep:
            text, sid, _ = c.get_text(messages="x", use_message_history=False)
        assert text == "ok"
        # Should have slept twice (after first 429, after second 429).
        assert mock_sleep.call_count == 2

    def test_gives_up_after_max_429_retries(self):
        c = self._client()
        # 4 attempts total (initial + 3 retries) — all 429.
        responses = [_fake_429_proc()] * 4
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            side_effect=responses,
        ), patch(
            "bizniz.clients.claude_cli.claude_cli_client.time.sleep"
        ):
            with pytest.raises(ClaudeCliClientError) as exc:
                c.get_text(messages="x", use_message_history=False)
        assert "rate-limited" in str(exc.value).lower()
        assert "Rate limited" in str(exc.value)

    def test_non_429_error_is_not_retried(self):
        """Non-429 errors (e.g. parse failures) bubble immediately
        — don't waste time backing off for a fundamentally broken
        request."""
        c = self._client()
        bad = _fake_proc("x", returncode=2)
        bad.stderr = "boom"
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run",
            return_value=bad,
        ) as m, patch(
            "bizniz.clients.claude_cli.claude_cli_client.time.sleep"
        ) as mock_sleep:
            with pytest.raises(ClaudeCliClientError):
                c.get_text(messages="x", use_message_history=False)
        # Only one subprocess call — no retry.
        assert m.call_count == 1
        assert mock_sleep.call_count == 0


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
