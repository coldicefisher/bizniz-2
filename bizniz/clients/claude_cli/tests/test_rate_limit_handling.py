"""Tests for the Max-plan usage-cap retry path and --fallback-model wiring.

Two features under test:

  1. ``_parse_usage_cap_reset`` parses ``"resets HH:MMam (TZ)"`` from
     429 bodies and returns seconds-until-reset. Distinct from
     transient 429s (which carry no reset time).
  2. ``ClaudeCliClient(fallback_model=...)`` appends
     ``--fallback-model <name>`` to the CLI invocation, so the CLI
     auto-switches when the primary is overloaded.

The retry-on-usage-cap behavior itself (sleep until reset, then retry
indefinitely) is integration-shaped — covered via a unit test that
stubs subprocess.run.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from bizniz.clients.claude_cli.claude_cli_client import (
    ClaudeCliClient,
    _parse_usage_cap_reset,
)


# ── parser ────────────────────────────────────────────────────────────


class TestParseUsageCapReset:
    def test_parses_standard_message(self):
        # "resets 11:20am (America/Los_Angeles)" → returns
        # positive seconds. Pin "now" via patching so test is stable.
        body = "You've hit your limit · resets 11:20am (America/Los_Angeles)"
        # Compute expected: 11:20 PT - now PT, plus 30s buffer.
        tz = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 5, 15, 10, 50, tzinfo=tz)
        with patch(
            "bizniz.clients.claude_cli.retry.datetime"
        ) as m:
            m.now.return_value = fake_now
            # Keep the real datetime constructor usable for the
            # ``now.replace(...)`` call inside the parser.
            m.side_effect = lambda *a, **kw: datetime(*a, **kw)
            wait = _parse_usage_cap_reset(body)
        # 10:50 → 11:20 = 30min, plus 30s buffer = 1830s.
        assert wait is not None
        assert 1820 <= wait <= 1840

    def test_parses_pm(self):
        body = "rate-limited; resets 3:05pm (America/Los_Angeles)"
        tz = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 5, 15, 14, 0, tzinfo=tz)
        with patch(
            "bizniz.clients.claude_cli.retry.datetime"
        ) as m:
            m.now.return_value = fake_now
            m.side_effect = lambda *a, **kw: datetime(*a, **kw)
            wait = _parse_usage_cap_reset(body)
        # 14:00 → 15:05 = 65min + 30s = 3930s
        assert wait is not None
        assert 3920 <= wait <= 3940

    def test_handles_12am_correctly(self):
        # 12:00am = midnight = hour 0. If "now" is 11pm same day, the
        # 12am is tomorrow.
        body = "resets 12:00am (UTC)"
        tz = ZoneInfo("UTC")
        fake_now = datetime(2026, 5, 15, 23, 0, tzinfo=tz)
        with patch(
            "bizniz.clients.claude_cli.retry.datetime"
        ) as m:
            m.now.return_value = fake_now
            m.side_effect = lambda *a, **kw: datetime(*a, **kw)
            wait = _parse_usage_cap_reset(body)
        # 23:00 → 00:00 next day = 1h + 30s = 3630s.
        assert wait is not None
        assert 3620 <= wait <= 3640

    def test_past_time_assumes_tomorrow(self):
        # Reset time is 9:00am, "now" is 10:00am — that 9am was an
        # hour ago, so the next 9am is 23h away.
        body = "resets 9:00am (UTC)"
        tz = ZoneInfo("UTC")
        fake_now = datetime(2026, 5, 15, 10, 0, tzinfo=tz)
        with patch(
            "bizniz.clients.claude_cli.retry.datetime"
        ) as m:
            m.now.return_value = fake_now
            m.side_effect = lambda *a, **kw: datetime(*a, **kw)
            wait = _parse_usage_cap_reset(body)
        # 23h + 30s ≈ 82830s
        assert wait is not None
        assert 82800 <= wait <= 82900

    def test_just_past_time_treated_as_now(self):
        # Reset time was 30s ago — slightly-late message. We DON'T
        # want to wait 23h. Treat as "now" and add a small buffer.
        body = "resets 10:00am (UTC)"
        tz = ZoneInfo("UTC")
        # 30s past target → delta of -30. Parser allows -60s before
        # rolling to tomorrow.
        fake_now = datetime(2026, 5, 15, 10, 0, 30, tzinfo=tz)
        with patch(
            "bizniz.clients.claude_cli.retry.datetime"
        ) as m:
            m.now.return_value = fake_now
            m.side_effect = lambda *a, **kw: datetime(*a, **kw)
            wait = _parse_usage_cap_reset(body)
        # delta was -30, treated as 0, plus 30s buffer = 30s
        assert wait is not None
        assert 0 <= wait <= 60

    def test_returns_none_for_non_usage_cap_body(self):
        # Transient 429s have no reset-time string.
        assert _parse_usage_cap_reset("Rate limited") is None
        assert _parse_usage_cap_reset("") is None
        assert _parse_usage_cap_reset(None) is None  # type: ignore
        assert _parse_usage_cap_reset(
            "Request failed: 429 Too Many Requests"
        ) is None

    def test_no_timezone_uses_naive_local(self):
        # Parser must accept the bare form without a (TZ) suffix.
        body = "resets 5:00am"
        # No tz → datetime.now() (naive). Just confirm we get a
        # positive number; exact value is system-dependent.
        wait = _parse_usage_cap_reset(body)
        assert wait is not None
        assert wait > 0


# ── --fallback-model wiring ───────────────────────────────────────────


class TestFallbackModelWiring:
    def _make_client(self, **kwargs):
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.shutil.which",
            return_value="/usr/bin/claude",
        ):
            return ClaudeCliClient(**kwargs)

    def _run_one_call(self, client, mock_stdout):
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run"
        ) as run:
            run.return_value = MagicMock(
                returncode=0, stdout=mock_stdout, stderr="",
            )
            client.get_text(
                messages=[{"role": "user", "content": "hi"}],
                use_message_history=False,
            )
            return run.call_args.args[0]  # the cmd list

    def test_no_fallback_by_default(self):
        client = self._make_client()
        cmd = self._run_one_call(
            client,
            json.dumps({"result": "hello", "session_id": "s"}),
        )
        assert "--fallback-model" not in cmd

    def test_constructor_arg_adds_flag(self):
        client = self._make_client(fallback_model="claude-haiku-4-5")
        cmd = self._run_one_call(
            client,
            json.dumps({"result": "hello", "session_id": "s"}),
        )
        assert "--fallback-model" in cmd
        idx = cmd.index("--fallback-model")
        assert cmd[idx + 1] == "claude-haiku-4-5"

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv(
            "BIZNIZ_CLAUDE_FALLBACK_MODEL", "claude-haiku-4-5-20251001",
        )
        client = self._make_client()
        cmd = self._run_one_call(
            client,
            json.dumps({"result": "hello", "session_id": "s"}),
        )
        assert "--fallback-model" in cmd
        idx = cmd.index("--fallback-model")
        assert cmd[idx + 1] == "claude-haiku-4-5-20251001"

    def test_constructor_arg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("BIZNIZ_CLAUDE_FALLBACK_MODEL", "from-env")
        client = self._make_client(fallback_model="from-arg")
        cmd = self._run_one_call(
            client,
            json.dumps({"result": "hello", "session_id": "s"}),
        )
        idx = cmd.index("--fallback-model")
        assert cmd[idx + 1] == "from-arg"


# ── usage-cap retry loop (integration-ish) ────────────────────────────


class TestUsageCapRetry:
    def _make_client(self):
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.shutil.which",
            return_value="/usr/bin/claude",
        ):
            return ClaudeCliClient()

    def test_usage_cap_429_waits_then_retries(self, monkeypatch):
        # Cap the wait so the test runs fast (sleep is mocked; this
        # is the value passed to it). The production default is 6h.
        monkeypatch.setenv("BIZNIZ_CLAUDE_USAGE_CAP_MAX_WAIT_S", "1")

        # First call: 429 with reset-time body. Second call: success.
        first = MagicMock(
            returncode=1,
            stdout=json.dumps({
                "api_error_status": 429,
                "result": "You've hit your limit · resets 11:20am (America/Los_Angeles)",
            }),
            stderr="",
        )
        second = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "got it", "session_id": "s"}),
            stderr="",
        )
        client = self._make_client()
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run"
        ) as run, patch(
            "bizniz.clients.claude_cli.claude_cli_client.time.sleep"
        ) as sleep:
            run.side_effect = [first, second]
            text, _, _ = client.get_text(
                messages=[{"role": "user", "content": "hi"}],
                use_message_history=False,
            )
        # Retried once after a sleep, got the success.
        assert text == "got it"
        assert run.call_count == 2
        # Slept at least once (the wait was capped to 0 but still
        # called sleep at least with chunk=0).
        assert sleep.call_count >= 1

    def test_transient_429_uses_short_backoff(self):
        # Body has no reset-time string → falls through to the
        # transient backoff (10/30/60s).
        first = MagicMock(
            returncode=1,
            stdout=json.dumps({
                "api_error_status": 429,
                "result": "Rate limited",
            }),
            stderr="",
        )
        second = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "got it", "session_id": "s"}),
            stderr="",
        )
        client = self._make_client()
        with patch(
            "bizniz.clients.claude_cli.claude_cli_client.subprocess.run"
        ) as run, patch(
            "bizniz.clients.claude_cli.claude_cli_client.time.sleep"
        ) as sleep:
            run.side_effect = [first, second]
            text, _, _ = client.get_text(
                messages=[{"role": "user", "content": "hi"}],
                use_message_history=False,
            )
        assert text == "got it"
        assert run.call_count == 2
        # The transient path slept exactly once with the first
        # backoff value (10s).
        sleep.assert_called_once_with(10.0)
