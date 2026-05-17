"""Tests for the generic multi-tier escalation primitive."""
from __future__ import annotations

from typing import List, Optional

import pytest

from bizniz.lib.tier_escalation import (
    AttemptOutcome, EscalationResult, TierSpec, escalate,
)


class _FakeAgent:
    """Stand-in 'agent' the factory hands back to the attempt fn."""
    def __init__(self, label: str):
        self.label = label


def _tier(label: str, attempts: int = 2) -> TierSpec[_FakeAgent]:
    return TierSpec(
        label=label,
        attempts=attempts,
        factory=lambda: _FakeAgent(label),
    )


def _always_succeeds(agent, ti, ai, prior):
    return AttemptOutcome(succeeded=True, output=f"{agent.label}:{ti}.{ai}")


def _always_fails(agent, ti, ai, prior):
    return AttemptOutcome(
        succeeded=False, output=f"fail at {agent.label}:{ti}.{ai}",
    )


# ── Tier validation ──────────────────────────────────────────────


class TestTierSpec:
    def test_attempts_must_be_positive(self):
        with pytest.raises(ValueError, match="attempts must be >= 1"):
            TierSpec(label="x", attempts=0, factory=lambda: _FakeAgent("x"))

    def test_attempts_negative_rejected(self):
        with pytest.raises(ValueError, match="attempts must be >= 1"):
            TierSpec(label="x", attempts=-1, factory=lambda: _FakeAgent("x"))


# ── escalate() top-level ─────────────────────────────────────────


class TestEscalate:
    def test_empty_tiers_rejected(self):
        with pytest.raises(ValueError, match="tiers list must be non-empty"):
            escalate([], _always_succeeds)

    def test_first_attempt_success_short_circuits(self):
        tiers = [_tier("cheap", 5), _tier("expensive", 5)]
        result = escalate(tiers, _always_succeeds)
        assert result.succeeded is True
        assert result.final_tier_index == 0
        assert result.total_attempts == 1
        # No tier-1 attempts.
        assert all(a.tier_index == 0 for a in result.attempts)

    def test_first_tier_exhausts_escalates_to_second(self):
        tiers = [_tier("cheap", 2), _tier("expensive", 2)]
        # First tier fails both; second tier succeeds first try.
        def fn(agent, ti, ai, prior):
            if ti == 0:
                return AttemptOutcome(succeeded=False, output="cheap fail")
            return AttemptOutcome(succeeded=True, output="expensive win")
        result = escalate(tiers, fn)
        assert result.succeeded is True
        assert result.final_tier_index == 1
        # 2 cheap fails + 1 expensive success = 3 total.
        assert result.total_attempts == 3

    def test_all_tiers_exhaust_returns_failure(self):
        tiers = [_tier("a", 2), _tier("b", 2), _tier("c", 2)]
        result = escalate(tiers, _always_fails)
        assert result.succeeded is False
        assert result.total_attempts == 6
        # Last tier label recorded.
        assert result.final_tier_label == "c"
        assert result.final_tier_index == 2

    def test_attempt_count_within_tier_respects_budget(self):
        tiers = [_tier("a", 3)]
        calls: List[int] = []
        def fn(agent, ti, ai, prior):
            calls.append(ai)
            return AttemptOutcome(succeeded=False, output="fail")
        escalate(tiers, fn)
        # 1-based attempt indices: 1, 2, 3.
        assert calls == [1, 2, 3]

    def test_prior_output_threaded(self):
        tiers = [_tier("a", 3)]
        priors: List[Optional[str]] = []
        def fn(agent, ti, ai, prior):
            priors.append(prior)
            return AttemptOutcome(succeeded=False, output=f"output-{ai}")
        escalate(tiers, fn)
        # First attempt sees None; subsequent sees prior outputs.
        assert priors[0] is None
        assert priors[1] == "output-1"
        assert priors[2] == "output-2"

    def test_prior_output_crosses_tier_boundary(self):
        tiers = [_tier("a", 1), _tier("b", 1)]
        priors: List[Optional[str]] = []
        def fn(agent, ti, ai, prior):
            priors.append(prior)
            return AttemptOutcome(succeeded=False, output=f"tier{ti}-out")
        escalate(tiers, fn)
        # Tier b's attempt sees tier a's output.
        assert priors[1] == "tier0-out"

    def test_each_tier_calls_factory_once(self):
        factory_calls: List[str] = []
        def make_tier(label):
            def factory():
                factory_calls.append(label)
                return _FakeAgent(label)
            return TierSpec(label=label, attempts=3, factory=factory)
        tiers = [make_tier("a"), make_tier("b")]
        escalate(tiers, _always_fails)
        # a + b each called once, even though 3 attempts per tier.
        assert factory_calls == ["a", "b"]

    def test_factory_failure_advances_to_next_tier(self):
        def bad_factory():
            raise RuntimeError("agent init blew up")
        tiers = [
            TierSpec(label="bad", attempts=2, factory=bad_factory),
            _tier("good", 2),
        ]
        result = escalate(tiers, _always_succeeds)
        # Bad tier's factory failure is recorded as one attempt with
        # an error output, then escalation advances and succeeds at "good".
        assert result.succeeded is True
        assert result.final_tier_label == "good"
        # First record is the factory-failure surrogate.
        assert "tier factory raised" in result.attempts[0].output


# ── Status callback ──────────────────────────────────────────────


class TestStatusCallback:
    def test_logs_emitted_per_tier_and_per_attempt(self):
        statuses: List[str] = []
        tiers = [_tier("a", 2), _tier("b", 1)]
        def fn(agent, ti, ai, prior):
            return AttemptOutcome(
                succeeded=(ti == 1), output="x",
            )
        escalate(tiers, fn, on_status=lambda m: statuses.append(m))
        joined = " ".join(statuses)
        assert "entering tier 0" in joined
        assert "entering tier 1" in joined
        # PASS at tier 1 attempt 1; fails at tier 0.
        assert any("PASS" in s for s in statuses)
        assert any("fail" in s for s in statuses)

    def test_buggy_callback_does_not_crash(self):
        def boom(_):
            raise RuntimeError("logger broke")
        tiers = [_tier("a", 1)]
        result = escalate(tiers, _always_succeeds, on_status=boom)
        assert result.succeeded is True


# ── Result shape ─────────────────────────────────────────────────


class TestEscalationResult:
    def test_attempt_history_complete(self):
        tiers = [_tier("a", 2), _tier("b", 2)]
        result = escalate(tiers, _always_fails)
        # 4 attempts total, all recorded.
        assert len(result.attempts) == 4
        assert [a.tier_index for a in result.attempts] == [0, 0, 1, 1]
        assert [a.attempt_index for a in result.attempts] == [1, 2, 1, 2]
        assert all(not a.succeeded for a in result.attempts)

    def test_final_output_is_last_attempt_output(self):
        tiers = [_tier("a", 1), _tier("b", 1)]
        def fn(agent, ti, ai, prior):
            return AttemptOutcome(succeeded=False, output=f"out-{ti}-{ai}")
        result = escalate(tiers, fn)
        assert result.final_output == "out-1-1"
