"""Tests for ``RefactorTestPhase`` (item 7A)."""
from __future__ import annotations

from typing import List, Optional

import pytest

from bizniz.driver.refactor_test_phase import (
    RefactorGitOps,
    RefactorTestPhase,
    RefactorTestPhaseResult,
    ServiceTestResult,
)
from bizniz.lib.tier_escalation import TierSpec


class _FakeGit(RefactorGitOps):
    def __init__(self):
        self.reverted_to: Optional[str] = None
    def revert_to(self, rev: str) -> None:
        self.reverted_to = rev


def _tier(label: str, attempts: int = 1) -> TierSpec:
    return TierSpec(label=label, attempts=attempts, factory=lambda: f"agent_{label}")


# ── Happy path ───────────────────────────────────────────────────


class TestHappyPath:
    def test_all_pass_first_try_no_repair(self):
        def run_tests(svc):
            return True, "all green"
        def repair_fn(agent, svc, output):
            pytest.fail("repair should not be called when tests pass")
        phase = RefactorTestPhase(
            services=["backend", "worker"],
            run_tests=run_tests,
            repair_tiers=[_tier("cheap")],
            repair_attempt_fn=repair_fn,
            git_ops=_FakeGit(),
        )
        result = phase.run()
        assert result.overall_passed is True
        assert result.services_reverted == 0
        for sr in result.service_results:
            assert sr.initial_test_passed
            assert sr.final_test_passed
            assert sr.repair_attempts_used == 0


# ── Repair escalation ────────────────────────────────────────────


class TestRepairEscalation:
    def test_first_tier_repairs_successfully(self):
        # Tests fail initially; cheap tier's one attempt fixes it.
        call_counts = {"tests": 0}
        def run_tests(svc):
            call_counts["tests"] += 1
            return False, "fail output"
        def repair_fn(agent, svc, output):
            # Repair succeeded — re-run tests would now pass.
            return True, "post-repair green"
        phase = RefactorTestPhase(
            services=["backend"],
            run_tests=run_tests,
            repair_tiers=[_tier("cheap"), _tier("expensive")],
            repair_attempt_fn=repair_fn,
            git_ops=_FakeGit(),
        )
        result = phase.run()
        assert result.overall_passed is True
        sr = result.service_results[0]
        assert sr.repair_succeeded is True
        assert sr.repair_tier_used == "cheap"
        assert sr.repair_attempts_used == 1
        assert sr.final_test_passed is True

    def test_cheap_tier_exhausts_expensive_recovers(self):
        def run_tests(svc):
            return False, "initial fail"
        attempts_per_label: List[str] = []
        def repair_fn(agent, svc, output):
            attempts_per_label.append(agent)
            # cheap agent's 2 attempts both fail; expensive succeeds.
            if agent == "agent_cheap":
                return False, "cheap couldn't fix"
            return True, "expensive fixed it"
        phase = RefactorTestPhase(
            services=["backend"],
            run_tests=run_tests,
            repair_tiers=[_tier("cheap", attempts=2),
                          _tier("expensive", attempts=2)],
            repair_attempt_fn=repair_fn,
            git_ops=_FakeGit(),
        )
        result = phase.run()
        assert result.overall_passed is True
        sr = result.service_results[0]
        assert sr.repair_tier_used == "expensive"
        # 2 cheap fails + 1 expensive success = 3 attempts.
        assert sr.repair_attempts_used == 3

    def test_all_tiers_exhaust_reverts_to_pre_rev(self):
        def run_tests(svc):
            return False, "always broken"
        def repair_fn(agent, svc, output):
            return False, "repair didn't help"
        git = _FakeGit()
        phase = RefactorTestPhase(
            services=["backend"],
            run_tests=run_tests,
            repair_tiers=[_tier("cheap"), _tier("expensive")],
            repair_attempt_fn=repair_fn,
            git_ops=git,
            pre_phase_rev="pre-refactor-rev",
        )
        result = phase.run()
        assert result.overall_passed is False
        sr = result.service_results[0]
        assert sr.final_test_passed is False
        # Git revert called once at end.
        assert git.reverted_to == "pre-refactor-rev"
        assert sr.reverted is True
        assert sr.revert_to_rev == "pre-refactor-rev"

    def test_no_pre_rev_does_not_revert(self):
        def run_tests(svc):
            return False, "fail"
        def repair_fn(agent, svc, output):
            return False, "still fail"
        git = _FakeGit()
        phase = RefactorTestPhase(
            services=["backend"],
            run_tests=run_tests,
            repair_tiers=[_tier("cheap")],
            repair_attempt_fn=repair_fn,
            git_ops=git,
            pre_phase_rev=None,
        )
        result = phase.run()
        assert result.overall_passed is False
        # Without a pre-rev, we don't revert.
        assert git.reverted_to is None

    def test_no_repair_tiers_wired(self):
        # Some services fail; no tiers configured → no repair attempted,
        # phase still reports overall_passed=False.
        def run_tests(svc):
            return svc == "ok", "..."
        phase = RefactorTestPhase(
            services=["broken", "ok"],
            run_tests=run_tests,
            repair_tiers=[],
            repair_attempt_fn=lambda a, s, o: pytest.fail("never called"),
            git_ops=_FakeGit(),
        )
        result = phase.run()
        assert result.overall_passed is False
        broken_sr = next(
            sr for sr in result.service_results if sr.service_name == "broken"
        )
        assert broken_sr.repair_attempts_used == 0


# ── Mixed services ───────────────────────────────────────────────


class TestMixedServices:
    def test_one_pass_one_recover_one_revert(self):
        def run_tests(svc):
            return {
                "passing_svc": (True, "green"),
                "recovers_svc": (False, "fail"),
                "broken_svc": (False, "fail"),
            }[svc]
        def repair_fn(agent, svc, output):
            # Recovers_svc gets fixed by cheap tier;
            # broken_svc stays broken.
            if svc == "recovers_svc":
                return True, "fixed"
            return False, "still bad"
        git = _FakeGit()
        phase = RefactorTestPhase(
            services=["passing_svc", "recovers_svc", "broken_svc"],
            run_tests=run_tests,
            repair_tiers=[_tier("cheap")],
            repair_attempt_fn=repair_fn,
            git_ops=git,
            pre_phase_rev="pre",
        )
        result = phase.run()
        assert result.overall_passed is False
        # Recovers passed via repair.
        recovers_sr = next(
            sr for sr in result.service_results
            if sr.service_name == "recovers_svc"
        )
        assert recovers_sr.final_test_passed is True
        # Whole phase reverts because ONE service stayed broken.
        assert git.reverted_to == "pre"
        # All services reverted as part of whole-phase revert? Only
        # the broken one, since recovers + passing were already green.
        # Implementation: revert is global, but services_reverted only
        # counts those that were NOT green at final time.
        assert result.services_reverted == 1


# ── Edge cases ───────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_services_short_circuits(self):
        phase = RefactorTestPhase(
            services=[],
            run_tests=lambda s: pytest.fail("never called"),
            repair_tiers=[_tier("cheap")],
            repair_attempt_fn=lambda a, s, o: pytest.fail("never"),
            git_ops=_FakeGit(),
        )
        result = phase.run()
        assert result.overall_passed is True
        assert result.skipped_reason == "no services to test"

    def test_revert_failure_does_not_crash(self):
        class _BadGit(RefactorGitOps):
            def revert_to(self, rev):
                raise RuntimeError("git is angry")
        def run_tests(svc):
            return False, "fail"
        def repair_fn(agent, svc, output):
            return False, "still fail"
        statuses: List[str] = []
        phase = RefactorTestPhase(
            services=["x"],
            run_tests=run_tests,
            repair_tiers=[_tier("cheap")],
            repair_attempt_fn=repair_fn,
            git_ops=_BadGit(),
            pre_phase_rev="pre",
            on_status=lambda m: statuses.append(m),
        )
        result = phase.run()
        # Revert raised — phase doesn't crash; logs the failure.
        assert result.overall_passed is False
        assert any("revert raised" in s for s in statuses)

    def test_on_status_callback_invoked(self):
        statuses: List[str] = []
        phase = RefactorTestPhase(
            services=["x"],
            run_tests=lambda s: (True, "ok"),
            repair_tiers=[_tier("cheap")],
            repair_attempt_fn=lambda a, s, o: (True, "ok"),
            git_ops=_FakeGit(),
            on_status=lambda m: statuses.append(m),
        )
        phase.run()
        joined = " ".join(statuses)
        assert "testing" in joined.lower()
        assert "passed first try" in joined.lower()
