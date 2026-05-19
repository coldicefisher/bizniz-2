"""Tests for v3.1 review/repair (2026-05-19).

v3.1 keeps V3's parallel QE+CR fan-out but drops the UnifiedFinding
adapter round-trip. Reports stay native; approval comes from
``QE.approved AND CR.approved`` (V2 semantics); repair dispatches the
V2 per-issue Coder loop. This file covers the same contract as
``test_review_repair_loop.py`` (the V2 loop) plus v3.1-specific
properties: branch selection, parallel call ordering, V2 semantic
preserved.
"""
from unittest.mock import MagicMock

import pytest

from bizniz.code_reviewer.types import CodeReviewReport, FlaggedSymbol
from bizniz.driver.gates import GatePolicy
from bizniz.driver.milestone_loop import MilestoneLoop
from bizniz.driver.state import MilestoneState
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


def _engineer_result(version: int = 0):
    """The repair loop just passes the EngineerResult around. Plan has
    an empty issues list so ``_phase_review_parallel`` doesn't try to
    open files on disk."""
    r = MagicMock()
    r.version = version
    r.plan = MagicMock()
    r.plan.issues = []
    r.plan.model_dump.return_value = {"issues": []}
    return r


def _arch():
    a = MagicMock()
    a.services = []
    return a


def _state(tmp_path):
    return MilestoneState(tmp_path / "m1", 1)


def _make_v3_1_loop(
    *,
    qe: MagicMock,
    cr: MagicMock,
    engineer: MagicMock,
    stall_threshold: int = 3,
    max_iterations: int = 20,
    code_dispatcher=None,
) -> MilestoneLoop:
    """Skeleton MilestoneLoop with v3.1 enabled."""
    loop = MilestoneLoop.__new__(MilestoneLoop)
    loop._qe = qe
    loop._cr = cr
    loop._engineer = engineer
    loop._gates = GatePolicy(mode="strict")
    loop._repair_stall_threshold = stall_threshold
    loop._repair_max_iterations = max_iterations
    loop._repair_engineer_factory = None
    loop._engineer_escalation_factory = None
    loop._code_dispatcher = code_dispatcher
    loop._issue_store_factory = None
    loop._workspace_summary = None
    loop._cost_tracker = None
    loop._on_status = None
    loop._use_v3_1 = True
    loop._use_v3_review_unit = False
    # _phase_review_parallel reads the primary workspace for file
    # snapshots. Empty issues list means no reads happen; supply a
    # stub anyway so attribute access doesn't fail.
    loop._primary_workspace = MagicMock()
    return loop


# ── Branch selection ────────────────────────────────────────────────


class TestBranchSelection:
    def test_use_v3_1_takes_precedence_over_v3_review_unit(self, tmp_path):
        """When both flags are set, v3.1 wins. v3 Stage B is
        deprecated; v3.1 is the canonical path."""
        qe = MagicMock()
        qe.review.return_value = _coverage(approved=True)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=True)
        eng = MagicMock()

        loop = _make_v3_1_loop(qe=qe, cr=cr, engineer=eng)
        # Set BOTH flags to confirm v3.1 path is taken.
        loop._use_v3_review_unit = True
        loop._use_v3_1 = True

        cov, crv, _result, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        # If v3 Stage B had been taken, it would have built a
        # ReviewUnitOrchestrator and the result wouldn't carry the
        # native QE.approved=True. v3.1 returns the QE/CR results
        # directly, so approval comes through unchanged.
        assert iters == 0
        assert cov.approved is True
        assert crv.approved is True

    def test_default_falls_through_to_v2_path(self, tmp_path):
        """When ``_use_v3_1`` is False, the v2 sequential path runs."""
        qe = MagicMock()
        qe.review.return_value = _coverage(approved=True)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=True)
        eng = MagicMock()

        loop = _make_v3_1_loop(qe=qe, cr=cr, engineer=eng)
        loop._use_v3_1 = False  # explicitly off → v2 path

        cov, crv, _r, iters, _ = loop._phase_review_repair_loop(
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


# ── Parallel review fan-out ────────────────────────────────────────


class TestParallelReview:
    def test_both_qe_and_cr_called_per_iteration(self, tmp_path):
        """Each loop iteration calls QE + CR exactly once. The
        parallel fan-out doesn't double-call either source (that was
        the v3 Stage B closure anti-pattern)."""
        qe = MagicMock()
        qe.review.side_effect = [
            _coverage(approved=False, missing_count=2),  # iter 0 initial
            _coverage(approved=True),                     # iter 1 after repair
        ]
        cr = MagicMock()
        cr.review.side_effect = [
            _code_review(approved=False, critical_count=1),
            _code_review(approved=True),
        ]
        eng = MagicMock()
        eng.repair.return_value = _engineer_result(1)

        loop = _make_v3_1_loop(qe=qe, cr=cr, engineer=eng)
        _cov, _crv, _r, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        # Initial review + one re-review = 2 calls each. No double-call.
        assert qe.review.call_count == 2
        assert cr.review.call_count == 2
        assert iters == 1

    def test_qe_exception_propagates(self, tmp_path):
        """If QE raises, the exception propagates — no silent
        adapter-layer swallowing. (V3 Stage B turned source failures
        into UnifiedFinding entries; v3.1 keeps V2's loud-fail
        semantics.)"""
        qe = MagicMock()
        qe.review.side_effect = RuntimeError("QE blew up")
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=True)
        eng = MagicMock()

        loop = _make_v3_1_loop(qe=qe, cr=cr, engineer=eng)
        with pytest.raises(RuntimeError, match="QE blew up"):
            loop._phase_review_repair_loop(
                state=_state(tmp_path),
                milestone=_milestone(),
                architecture=_arch(),
                spec=MagicMock(),
                initial_result=_engineer_result(),
                auth_contract=None,
                prior_list=[],
            )


# ── V2 approval semantics preserved ────────────────────────────────


class TestApprovalSemantics:
    def test_zero_iterations_when_initial_review_approves(self, tmp_path):
        """QE.approved=True AND CR.approved=True → done immediately,
        no repair. This is the core difference vs v3 Stage B, which
        ignored the per-source approval flag and looped until findings
        count hit zero."""
        qe = MagicMock()
        qe.review.return_value = _coverage(approved=True)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=True)
        eng = MagicMock()

        loop = _make_v3_1_loop(qe=qe, cr=cr, engineer=eng)
        cov, crv, _r, iters, history = loop._phase_review_repair_loop(
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

    def test_approved_with_nice_to_have_gaps_still_approves(self, tmp_path):
        """The exact bug v3 Stage B had: QE.approved=True with leftover
        nice-to-have ``missing_scenarios``. v3 Stage B's adapter
        emitted UnifiedFindings from those gaps and the loop stalled
        for 13 iterations even though both reviewers had approved.
        v3.1 must approve on the first pass."""
        qe = MagicMock()
        # approved=True but with 2 leftover missing_scenarios. V2
        # semantics: trust the approved flag.
        qe.review.return_value = _coverage(approved=True, missing_count=2)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=True)
        eng = MagicMock()

        loop = _make_v3_1_loop(qe=qe, cr=cr, engineer=eng)
        _cov, _crv, _r, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        assert iters == 0
        eng.repair.assert_not_called()


# ── V2 repair convergence ───────────────────────────────────────────


class TestRepairConvergence:
    def test_converges_after_one_repair(self, tmp_path):
        qe = MagicMock()
        qe.review.side_effect = [
            _coverage(approved=False, missing_count=2),
            _coverage(approved=True),
        ]
        cr = MagicMock()
        cr.review.side_effect = [
            _code_review(approved=False, critical_count=1),
            _code_review(approved=True),
        ]
        eng = MagicMock()
        eng.repair.return_value = _engineer_result(1)

        loop = _make_v3_1_loop(qe=qe, cr=cr, engineer=eng)
        _cov, _crv, _r, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        assert iters == 1
        assert eng.repair.call_count == 1

    def test_code_dispatcher_repair_used_when_set(self, tmp_path):
        """When ``code_dispatcher`` is set, its ``.repair`` is called
        (not the v2 Engineer's). Same dispatch path v2 uses today."""
        qe = MagicMock()
        qe.review.side_effect = [
            _coverage(approved=False, missing_count=1),
            _coverage(approved=True),
        ]
        cr = MagicMock()
        cr.review.side_effect = [
            _code_review(approved=False),
            _code_review(approved=True),
        ]
        eng = MagicMock()
        dispatcher = MagicMock()
        dispatcher.repair.return_value = _engineer_result(1)

        loop = _make_v3_1_loop(
            qe=qe, cr=cr, engineer=eng, code_dispatcher=dispatcher,
        )
        _cov, _crv, _r, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        assert iters == 1
        # Dispatcher repair called, not Engineer.repair.
        assert dispatcher.repair.call_count == 1
        eng.repair.assert_not_called()


# ── Stall + hard cap (V2 semantics) ────────────────────────────────


class TestStallAndCap:
    def test_stalled_halts_at_threshold(self, tmp_path):
        qe = MagicMock()
        qe.review.return_value = _coverage(approved=False, missing_count=2)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=False)
        eng = MagicMock()
        eng.repair.return_value = _engineer_result(1)

        loop = _make_v3_1_loop(
            qe=qe, cr=cr, engineer=eng, stall_threshold=3,
        )
        cov, _crv, _r, iters, _ = loop._phase_review_repair_loop(
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

    def test_hard_cap_caps_runaway(self, tmp_path):
        qe = MagicMock()
        defect_seq = [100 - i for i in range(50)]
        qe.review.side_effect = [
            _coverage(approved=False, missing_count=n) for n in defect_seq
        ]
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=False)
        eng = MagicMock()
        eng.repair.return_value = _engineer_result(1)

        loop = _make_v3_1_loop(
            qe=qe, cr=cr, engineer=eng,
            stall_threshold=20, max_iterations=5,
        )
        _cov, _crv, _r, iters, _ = loop._phase_review_repair_loop(
            state=_state(tmp_path),
            milestone=_milestone(),
            architecture=_arch(),
            spec=MagicMock(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
        )
        assert iters == 5
