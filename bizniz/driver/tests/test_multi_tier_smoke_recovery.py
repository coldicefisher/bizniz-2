"""Tests for ``MultiTierSmokeRecovery`` (item 7C)."""
from __future__ import annotations

from typing import List
from unittest.mock import MagicMock

import pytest

from bizniz.driver.smoke_recovery import (
    MultiTierRecoveryResult,
    MultiTierSmokeRecovery,
    SmokeRecovery,
    SmokeRecoveryResult,
)
from bizniz.lib.tier_escalation import TierSpec


def _fake_recovery(succeeded: bool = True, summary: str = "ok") -> SmokeRecovery:
    """Build a SmokeRecovery-shaped mock that just returns the given result."""
    sr = MagicMock(spec=SmokeRecovery)
    sr.recover.return_value = SmokeRecoveryResult(
        attempted=True, succeeded=succeeded, summary=summary,
    )
    return sr


def _tier(label: str, recovery: SmokeRecovery, attempts: int = 1) -> TierSpec:
    return TierSpec(
        label=label, attempts=attempts,
        factory=lambda: recovery,
    )


# ── Construction guards ──────────────────────────────────────────


class TestConstruction:
    def test_empty_tiers_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            MultiTierSmokeRecovery(
                tiers=[], verify_smoke=lambda: True,
            )


# ── Happy paths ──────────────────────────────────────────────────


class TestRecover:
    def test_first_tier_succeeds_immediately(self):
        sr = _fake_recovery()
        smoke_passes = [True]
        mt = MultiTierSmokeRecovery(
            tiers=[_tier("cheap", sr), _tier("expensive", _fake_recovery())],
            verify_smoke=lambda: smoke_passes.pop(0),
        )
        result = mt.recover(
            critical_failures=["route 500"], service_names=["backend"],
            milestone_title="M5",
        )
        assert result.succeeded is True
        assert result.final_tier_label == "cheap"
        assert result.total_attempts == 1
        # Recovery agent called exactly once.
        assert sr.recover.call_count == 1

    def test_cheap_fails_expensive_recovers(self):
        cheap = _fake_recovery(succeeded=False, summary="cheap couldn't fix")
        expensive = _fake_recovery(succeeded=True, summary="expensive fixed")
        # Verify: false after cheap, true after expensive.
        verifications = [False, True]
        mt = MultiTierSmokeRecovery(
            tiers=[
                _tier("cheap", cheap),
                _tier("expensive", expensive),
            ],
            verify_smoke=lambda: verifications.pop(0),
        )
        result = mt.recover(
            critical_failures=["route 500"], service_names=["backend"],
            milestone_title="M5",
        )
        assert result.succeeded is True
        assert result.final_tier_label == "expensive"
        assert result.total_attempts == 2

    def test_all_tiers_exhaust_returns_failure(self):
        cheap = _fake_recovery(succeeded=False)
        expensive = _fake_recovery(succeeded=False)
        verifications = [False, False, False]
        mt = MultiTierSmokeRecovery(
            tiers=[
                _tier("cheap", cheap, attempts=2),
                _tier("expensive", expensive, attempts=1),
            ],
            verify_smoke=lambda: verifications.pop(0) if verifications else False,
        )
        result = mt.recover(
            critical_failures=["x"], service_names=["backend"],
            milestone_title="M5",
        )
        assert result.succeeded is False
        assert result.total_attempts == 3
        assert len(result.tier_history) == 3

    def test_within_tier_multiple_attempts(self):
        sr = _fake_recovery()
        # First two verifications False (within-tier retries fail),
        # third True.
        verifications = [False, False, True]
        mt = MultiTierSmokeRecovery(
            tiers=[_tier("cheap", sr, attempts=3)],
            verify_smoke=lambda: verifications.pop(0),
        )
        result = mt.recover(
            critical_failures=["x"], service_names=["x"],
            milestone_title="m",
        )
        assert result.succeeded is True
        assert result.total_attempts == 3
        # Same SmokeRecovery agent reused — factory called once.
        assert sr.recover.call_count == 3


# ── Edge cases ───────────────────────────────────────────────────


class TestEdgeCases:
    def test_verify_smoke_raising_does_not_crash(self):
        sr = _fake_recovery()
        def bad_verify():
            raise RuntimeError("compose status lookup blew up")
        mt = MultiTierSmokeRecovery(
            tiers=[_tier("cheap", sr)],
            verify_smoke=bad_verify,
        )
        result = mt.recover(
            critical_failures=["x"], service_names=["x"],
            milestone_title="m",
        )
        # verify_smoke raised — treated as smoke-not-passing → escalation
        # exhausts and we return failure (not a crash).
        assert result.succeeded is False

    def test_recovery_passes_inputs_through_to_inner(self):
        sr = _fake_recovery()
        mt = MultiTierSmokeRecovery(
            tiers=[_tier("cheap", sr)],
            verify_smoke=lambda: True,
        )
        mt.recover(
            critical_failures=["route 500", "auth 401"],
            service_names=["backend", "auth"],
            milestone_title="M5",
        )
        # The inner SmokeRecovery sees the same args we passed.
        sr.recover.assert_called_once_with(
            critical_failures=["route 500", "auth 401"],
            service_names=["backend", "auth"],
            milestone_title="M5",
        )

    def test_status_callback_invoked(self):
        sr = _fake_recovery()
        statuses: List[str] = []
        mt = MultiTierSmokeRecovery(
            tiers=[_tier("cheap", sr)],
            verify_smoke=lambda: True,
            on_status=lambda m: statuses.append(m),
        )
        mt.recover(
            critical_failures=["x"], service_names=["x"],
            milestone_title="m",
        )
        joined = " ".join(statuses)
        assert "tier 0" in joined
        assert "cheap" in joined

    def test_tier_history_captures_pass_fail_per_attempt(self):
        cheap = _fake_recovery()
        verifications = [False, True]   # cheap attempt 1 fail, 2 pass
        mt = MultiTierSmokeRecovery(
            tiers=[_tier("cheap", cheap, attempts=2)],
            verify_smoke=lambda: verifications.pop(0),
        )
        result = mt.recover(
            critical_failures=["x"], service_names=["x"],
            milestone_title="m",
        )
        assert len(result.tier_history) == 2
        assert "fail" in result.tier_history[0]
        assert "PASS" in result.tier_history[1]
