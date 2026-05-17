"""Tests for ``call_with_retry``'s transient-vs-permanent retry logic."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.errors import AIInsufficientFunds
from bizniz.lib.llm_utils import (
    LLMCallError,
    _is_transient,
    _resolve_transient_backoff,
    call_with_retry,
)


# ── _is_transient classification ─────────────────────────────────


class TestIsTransient:
    @pytest.mark.parametrize("msg", [
        # Live ClaudeCliClient shape from the CRM v1 M5 outage 2026-05-16
        'API Error: 500 Internal server error. This is a server-side issue.',
        '"api_error_status":500,"duration_ms":12997',
        '"api_error_status": 503',
        "API Error: 502 Bad Gateway",
        "Service Unavailable",
        "Gateway Timeout",
        "Internal server error",
        "Connection reset by peer",
        "Connection refused",
        "Connection aborted",
        "Read timeout after 30s",
        "request timed out",
        "Remote end closed connection without response",
    ])
    def test_transient_patterns(self, msg):
        assert _is_transient(Exception(msg)) is True

    @pytest.mark.parametrize("msg", [
        "JSON schema violation: missing key 'foo'",
        "ValueError: not valid JSON",
        "ServicePlannerError: depends_on references unknown id",
        "Authentication failed",
        "Empty response from AI",
    ])
    def test_permanent_patterns(self, msg):
        assert _is_transient(Exception(msg)) is False


# ── Fixtures ─────────────────────────────────────────────────────


def _client_returning(payload):
    c = MagicMock(spec=BaseAIClient)
    c.get_text.return_value = (json.dumps(payload), "j", [])
    return c


def _client_sequence(returns_or_raises):
    """Each entry is either an Exception (will be raised) or a dict
    (will be returned as the next payload)."""
    c = MagicMock(spec=BaseAIClient)
    iterator = iter(returns_or_raises)

    def side(*args, **kwargs):
        item = next(iterator)
        if isinstance(item, BaseException):
            raise item
        return (json.dumps(item), "j", [])

    c.get_text.side_effect = side
    return c


# ── Happy path (unchanged behavior) ──────────────────────────────


class TestHappyPath:
    def test_returns_on_first_success(self):
        client = _client_returning({"ok": 1})
        result = call_with_retry(client=client, messages=[])
        assert result == {"ok": 1}
        assert client.get_text.call_count == 1


# ── Permanent failure path ───────────────────────────────────────


class TestPermanentFailures:
    def test_three_strikes_raises(self):
        client = _client_sequence([
            ValueError("not valid JSON"),
            ValueError("not valid JSON"),
            ValueError("not valid JSON"),
        ])
        sleeps: list = []
        with pytest.raises(LLMCallError, match="3 permanent"):
            call_with_retry(
                client=client, messages=[],
                sleep=lambda s: sleeps.append(s),
            )
        # Permanent errors do NOT trigger backoff sleeps.
        assert sleeps == []

    def test_empty_response_treated_as_permanent(self):
        c = MagicMock(spec=BaseAIClient)
        c.get_text.return_value = ("", "j", [])
        sleeps: list = []
        with pytest.raises(LLMCallError):
            call_with_retry(
                client=c, messages=[],
                sleep=lambda s: sleeps.append(s),
            )
        assert sleeps == []
        assert c.get_text.call_count == 3

    def test_permanent_then_success(self):
        client = _client_sequence([
            ValueError("bad JSON"),
            {"ok": 1},
        ])
        result = call_with_retry(client=client, messages=[])
        assert result == {"ok": 1}

    def test_insufficient_funds_reraised_immediately(self):
        client = _client_sequence([
            AIInsufficientFunds("out of credits"),
            {"ok": 1},  # Should never be reached
        ])
        with pytest.raises(AIInsufficientFunds):
            call_with_retry(client=client, messages=[])
        assert client.get_text.call_count == 1


# ── Transient failure path (the new behavior) ────────────────────


class TestTransientFailures:
    def test_transient_then_success_applies_backoff(self):
        client = _client_sequence([
            Exception("API Error: 500 Internal server error"),
            {"ok": 1},
        ])
        sleeps: list = []
        result = call_with_retry(
            client=client, messages=[],
            transient_backoff_s=(7.0, 11.0, 13.0),
            sleep=lambda s: sleeps.append(s),
        )
        assert result == {"ok": 1}
        # One transient → one sleep, using first backoff entry.
        assert sleeps == [7.0]

    def test_transient_backoff_advances_through_schedule(self):
        client = _client_sequence([
            Exception("API Error: 500"),
            Exception("API Error: 502 Bad Gateway"),
            Exception("Internal server error"),
            {"ok": 1},
        ])
        sleeps: list = []
        result = call_with_retry(
            client=client, messages=[],
            transient_backoff_s=(2.0, 4.0, 8.0, 16.0),
            sleep=lambda s: sleeps.append(s),
        )
        assert result == {"ok": 1}
        # Three transients → first three backoff entries.
        assert sleeps == [2.0, 4.0, 8.0]

    def test_transient_budget_exhausted_raises(self):
        client = _client_sequence([
            Exception("API Error: 500")
            for _ in range(20)
        ])
        sleeps: list = []
        with pytest.raises(LLMCallError, match="transient"):
            call_with_retry(
                client=client, messages=[],
                max_transient_attempts=3,
                transient_backoff_s=(1.0, 1.0, 1.0),
                sleep=lambda s: sleeps.append(s),
            )
        # Three transients = 2 sleeps (no sleep on the final fail-out).
        assert sleeps == [1.0, 1.0]

    def test_transient_then_permanent_separate_budgets(self):
        # Two transient + two permanent should NOT exhaust either
        # budget if budgets are 3 each — should not raise.
        client = _client_sequence([
            Exception("API Error: 500"),
            Exception("Internal server error"),
            ValueError("bad JSON"),
            ValueError("bad JSON"),
            {"ok": 1},
        ])
        sleeps: list = []
        result = call_with_retry(
            client=client, messages=[],
            max_attempts=3, max_transient_attempts=3,
            transient_backoff_s=(1.0, 1.0, 1.0),
            sleep=lambda s: sleeps.append(s),
        )
        assert result == {"ok": 1}
        # Two transients = two sleeps (none for the permanent ones).
        assert sleeps == [1.0, 1.0]

    def test_backoff_schedule_indexes_clamp_at_end(self):
        # Six transients but only 2 backoff entries — should clamp to
        # the last entry rather than crash.
        client = _client_sequence([
            Exception("API Error: 500"),
            Exception("API Error: 500"),
            Exception("API Error: 500"),
            Exception("API Error: 500"),
            {"ok": 1},
        ])
        sleeps: list = []
        result = call_with_retry(
            client=client, messages=[],
            max_transient_attempts=10,
            transient_backoff_s=(1.0, 5.0),
            sleep=lambda s: sleeps.append(s),
        )
        assert result == {"ok": 1}
        # 4 transients = 4 sleeps, clamped to last entry after index 1.
        assert sleeps == [1.0, 5.0, 5.0, 5.0]

    def test_status_callback_distinguishes_transient(self):
        client = _client_sequence([
            Exception("API Error: 500"),
            {"ok": 1},
        ])
        statuses: list = []
        call_with_retry(
            client=client, messages=[],
            transient_backoff_s=(1.0,),
            sleep=lambda s: None,
            on_status=lambda m: statuses.append(m),
            label="TestAgent",
        )
        # At least one status line should mention "transient" for the
        # 500, and mention the backoff time.
        assert any("transient" in s.lower() for s in statuses)
        assert any("1s" in s or "1.0" in s.replace(" ", "") for s in statuses)


# ── Env-var override surface ─────────────────────────────────────


class TestEnvVarOverride:
    """Test ``_resolve_transient_backoff`` directly to avoid the
    module reload that would orphan ``LLMCallError`` class identity
    for sibling tests."""

    def test_env_var_override_parses(self, monkeypatch):
        monkeypatch.setenv("BIZNIZ_TRANSIENT_BACKOFF_S", "5, 10, 20")
        assert _resolve_transient_backoff() == (5.0, 10.0, 20.0)

    def test_env_var_empty_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("BIZNIZ_TRANSIENT_BACKOFF_S", "")
        assert _resolve_transient_backoff() == (
            30.0, 90.0, 300.0, 600.0, 1800.0, 3600.0,
        )

    def test_env_var_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("BIZNIZ_TRANSIENT_BACKOFF_S", "abc,xyz")
        assert _resolve_transient_backoff() == (
            30.0, 90.0, 300.0, 600.0, 1800.0, 3600.0,
        )

    def test_env_var_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("BIZNIZ_TRANSIENT_BACKOFF_S", raising=False)
        assert _resolve_transient_backoff() == (
            30.0, 90.0, 300.0, 600.0, 1800.0, 3600.0,
        )


# ── Perf emitter integration (roadmap item 9 Phase 2B) ───────────


class TestPerfEmitterIntegration:
    def test_successful_call_emits_agent_call(self):
        from bizniz.perf_log.emitter import PerfEmitter, AgentCallEvent
        client = _client_returning({"ok": 1})
        em = PerfEmitter(in_memory=True)
        result = call_with_retry(
            client=client, messages=[],
            label="Planner", target="m1", perf_emitter=em,
        )
        assert result == {"ok": 1}
        calls = [
            ev for ev in em.collected if isinstance(ev, AgentCallEvent)
        ]
        assert len(calls) == 1
        assert calls[0].agent == "Planner"
        assert calls[0].target == "m1"
        assert calls[0].succeeded is True
        assert calls[0].permanent_attempts == 0
        assert calls[0].transient_attempts == 0

    def test_permanent_retry_emits_agent_retry(self):
        from bizniz.perf_log.emitter import (
            PerfEmitter, AgentCallEvent, AgentRetryEvent,
        )
        client = _client_sequence([
            ValueError("not valid JSON"),
            {"ok": 1},
        ])
        em = PerfEmitter(in_memory=True)
        call_with_retry(
            client=client, messages=[],
            label="Architect", perf_emitter=em,
        )
        retries = [ev for ev in em.collected if isinstance(ev, AgentRetryEvent)]
        assert len(retries) == 1
        assert retries[0].classification == "permanent"
        assert "not valid JSON" in retries[0].error
        # Final agent_call event has permanent_attempts=1.
        final = [ev for ev in em.collected if isinstance(ev, AgentCallEvent)]
        assert final[0].permanent_attempts == 1

    def test_transient_retry_emits_with_wait_s(self):
        from bizniz.perf_log.emitter import (
            PerfEmitter, AgentRetryEvent,
        )
        client = _client_sequence([
            Exception("API Error: 500"),
            {"ok": 1},
        ])
        em = PerfEmitter(in_memory=True)
        call_with_retry(
            client=client, messages=[],
            label="Planner",
            transient_backoff_s=(0.001,),
            sleep=lambda _: None,
            perf_emitter=em,
        )
        retries = [ev for ev in em.collected if isinstance(ev, AgentRetryEvent)]
        assert len(retries) == 1
        assert retries[0].classification == "transient"
        assert retries[0].wait_s > 0

    def test_exhausted_budget_emits_failure_agent_call(self):
        from bizniz.perf_log.emitter import (
            PerfEmitter, AgentCallEvent, AgentRetryEvent,
        )
        client = _client_sequence([
            ValueError("bad JSON") for _ in range(5)
        ])
        em = PerfEmitter(in_memory=True)
        with pytest.raises(LLMCallError):
            call_with_retry(
                client=client, messages=[],
                label="Planner", perf_emitter=em,
            )
        # Final agent_call event has succeeded=False.
        finals = [
            ev for ev in em.collected if isinstance(ev, AgentCallEvent)
        ]
        assert len(finals) == 1
        assert finals[0].succeeded is False
        # All 3 permanent attempts emitted retries.
        retries = [
            ev for ev in em.collected if isinstance(ev, AgentRetryEvent)
        ]
        assert len(retries) == 3

    def test_no_emitter_does_not_crash(self):
        # Defaults to None — proves backward compat.
        client = _client_returning({"ok": 1})
        result = call_with_retry(
            client=client, messages=[],
            label="Planner",
            # perf_emitter omitted — should still work
        )
        assert result == {"ok": 1}
