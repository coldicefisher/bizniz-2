"""Tests for the progress-based review/repair loop (D5, 2026-05-17).

Verifies the contract for ``MilestoneLoop._phase_review_repair_loop``:

- Initial review approval → 0 repair iterations, no Engineer.repair calls
- Single failing review + recovering review → 1 repair iteration
- Defects decreasing across iters → keep looping (no early halt)
- Stalled (defects flat) → halt at stall threshold
- Regression (defects up) → counts toward stall threshold
- Progress resets the stall counter
- Hard cap (repair_max_iterations) prevents runaway when defect count
  keeps shrinking by one forever
"""
from unittest.mock import MagicMock

import pytest

from bizniz.code_reviewer.types import CodeReviewReport, FlaggedSymbol
from bizniz.driver.gates import GatePolicy, GateViolation
from bizniz.driver.milestone_loop import MilestoneLoop
from bizniz.driver.state import MilestoneState, SubPhase
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.types import CoverageReport, MissingScenario


def _milestone() -> Milestone:
    return Milestone(
        sequence_index=0,
        name="test milestone",
        problem_slice="do the thing",
        use_cases=["x"],
        success_criteria=["done"],
    )


def _coverage(*, approved: bool, missing_count: int = 0) -> CoverageReport:
    return CoverageReport(
        milestone_name="test milestone",
        approved=approved,
        missing_scenarios=[
            MissingScenario(capability_id="c1", scenario=f"s{i}")
            for i in range(missing_count)
        ],
    )


def _code_review(*, approved: bool, critical_count: int = 0) -> CodeReviewReport:
    return CodeReviewReport(
        milestone_name="test milestone",
        approved=approved,
        flagged_symbols=[
            FlaggedSymbol(
                file="x.py",
                symbol=f"y{i}",
                kind="import",
                reason="bad",
                severity="critical",
            )
            for i in range(critical_count)
        ],
    )


def _make_loop_skeleton(
    *,
    qe: MagicMock,
    cr: MagicMock,
    engineer: MagicMock,
    stall_threshold: int = 3,
    max_iterations: int = 20,
    gates: GatePolicy = None,
) -> MilestoneLoop:
    """Skeleton MilestoneLoop with only the surface the review/repair
    loop touches. Same pattern as test_confidence_signals.py."""
    loop = MilestoneLoop.__new__(MilestoneLoop)
    loop._qe = qe
    loop._cr = cr
    loop._engineer = engineer
    loop._gates = gates or GatePolicy(mode="strict")
    loop._repair_stall_threshold = stall_threshold
    loop._repair_max_iterations = max_iterations
    loop._repair_engineer_factory = None
    loop._engineer_escalation_factory = None
    loop._code_dispatcher = None
    loop._issue_store_factory = None
    loop._workspace_summary = None
    loop._cost_tracker = None
    loop._on_status = None
    return loop


def _arch():
    a = MagicMock()
    a.services = []
    return a


def _state(tmp_path):
    return MilestoneState(tmp_path / "m1", 1)


def _engineer_result(version: int = 0):
    """The repair loop just passes the EngineerResult around — Engineer
    mocks return whatever; we identity-track via a sentinel object."""
    r = MagicMock()
    r.version = version
    return r


class TestInitialApproval:
    def test_zero_iterations_when_initial_review_approves(self, tmp_path):
        qe = MagicMock()
        qe.review.return_value = _coverage(approved=True)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=True)
        eng = MagicMock()

        loop = _make_loop_skeleton(qe=qe, cr=cr, engineer=eng)
        cov, crv, result, iters, history = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        assert iters == 0
        assert cov.approved is True
        assert crv.approved is True
        eng.repair.assert_not_called()
        assert history == ""


class TestProgressBehavior:
    def test_converges_after_one_repair(self, tmp_path):
        qe = MagicMock()
        qe.review.side_effect = [
            _coverage(approved=False, missing_count=2),  # initial
            _coverage(approved=True),                     # after iter 1
        ]
        cr = MagicMock()
        cr.review.side_effect = [
            _code_review(approved=False, critical_count=1),
            _code_review(approved=True),
        ]
        eng = MagicMock()
        eng.repair.return_value = _engineer_result(1)

        loop = _make_loop_skeleton(qe=qe, cr=cr, engineer=eng)
        cov, crv, result, iters, history = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        assert iters == 1
        assert cov.approved is True
        assert eng.repair.call_count == 1

    def test_defects_decreasing_keeps_looping(self, tmp_path):
        # Defects: 3 → 2 → 1 → 0 (approved). With threshold=2, the loop
        # would halt at iter 2 if any iter stalled. But progress every
        # time → loop runs to convergence at iter 3.
        qe = MagicMock()
        qe.review.side_effect = [
            _coverage(approved=False, missing_count=3),
            _coverage(approved=False, missing_count=2),
            _coverage(approved=False, missing_count=1),
            _coverage(approved=True),
        ]
        cr = MagicMock()
        cr.review.side_effect = [
            _code_review(approved=False),
            _code_review(approved=False),
            _code_review(approved=False),
            _code_review(approved=True),
        ]
        eng = MagicMock()
        eng.repair.return_value = _engineer_result(1)

        loop = _make_loop_skeleton(
            qe=qe, cr=cr, engineer=eng, stall_threshold=2,
        )
        cov, crv, result, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        assert iters == 3
        assert cov.approved is True


class TestStallBehavior:
    def test_stalled_halts_at_threshold(self, tmp_path):
        # Defects flat at 2 every iter. threshold=3 → 3 stalled iters
        # then halt with un-approved result.
        qe = MagicMock()
        qe.review.return_value = _coverage(approved=False, missing_count=2)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=False)
        eng = MagicMock()
        eng.repair.return_value = _engineer_result(1)

        loop = _make_loop_skeleton(
            qe=qe, cr=cr, engineer=eng, stall_threshold=3,
        )
        cov, crv, _, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        assert iters == 3
        assert cov.approved is False

    def test_regression_counts_toward_stall(self, tmp_path):
        # Defects 1 → 2 → 3 (each iter makes it worse). threshold=2 →
        # halts after 2 regressions.
        qe = MagicMock()
        qe.review.side_effect = [
            _coverage(approved=False, missing_count=1),
            _coverage(approved=False, missing_count=2),
            _coverage(approved=False, missing_count=3),
        ]
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=False)
        eng = MagicMock()
        eng.repair.return_value = _engineer_result(1)

        loop = _make_loop_skeleton(
            qe=qe, cr=cr, engineer=eng, stall_threshold=2,
        )
        cov, crv, _, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        assert iters == 2
        assert len(cov.missing_scenarios) == 3

    def test_progress_resets_stall_counter(self, tmp_path):
        # Pattern: stalled, stalled, PROGRESS, stalled, stalled, stalled
        # With threshold=3, early stalls would have halted at iter 3 if
        # the progress at iter 3 didn't reset. Loop should run to iter 6.
        qe = MagicMock()
        qe.review.side_effect = [
            _coverage(approved=False, missing_count=2),  # initial
            _coverage(approved=False, missing_count=2),  # iter 1: stalled (1)
            _coverage(approved=False, missing_count=2),  # iter 2: stalled (2)
            _coverage(approved=False, missing_count=1),  # iter 3: progress (RESET)
            _coverage(approved=False, missing_count=1),  # iter 4: stalled (1)
            _coverage(approved=False, missing_count=1),  # iter 5: stalled (2)
            _coverage(approved=False, missing_count=1),  # iter 6: stalled (3 → halt)
        ]
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=False)
        eng = MagicMock()
        eng.repair.return_value = _engineer_result(1)

        loop = _make_loop_skeleton(
            qe=qe, cr=cr, engineer=eng, stall_threshold=3,
        )
        cov, crv, _, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        # Reset working = 6 iters; broken = 3 iters.
        assert iters == 6


class TestHardCap:
    def test_max_iterations_kills_runaway(self, tmp_path):
        # Decrease defect count by 1 every iter (always "progress"),
        # never converge, never approve. Without the hard cap the loop
        # runs forever. With max_iterations=5 it halts at 5.
        qe = MagicMock()
        defect_seq = [100 - i for i in range(50)]
        qe.review.side_effect = [
            _coverage(approved=False, missing_count=n) for n in defect_seq
        ]
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=False)
        eng = MagicMock()
        eng.repair.return_value = _engineer_result(1)

        loop = _make_loop_skeleton(
            qe=qe, cr=cr, engineer=eng,
            stall_threshold=20, max_iterations=5,
        )
        cov, crv, _, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        assert iters == 5
