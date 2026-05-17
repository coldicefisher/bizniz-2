"""Tests for the iterative smoke-recovery loop in
``MilestoneLoop._maybe_recover_smoke`` (D3, 2026-05-17).

Confirms the progress-based stopping contract:
- Failures decreasing → keep iterating
- Convergence (failures==0) → stop immediately
- Stall threshold reached (N consecutive no-progress iters) → stop
- Passing input → no recovery dispatched
- No SmokeRecovery injected → no-op pass-through
"""
from unittest.mock import MagicMock

from bizniz.driver.milestone_loop import MilestoneLoop
from bizniz.driver.smoke_phase import SmokePhaseResult
from bizniz.driver.smoke_recovery import SmokeRecoveryResult


def _result(critical: list[str]) -> SmokePhaseResult:
    return SmokePhaseResult(
        passed=(not critical),
        critical_failures=list(critical),
    )


def _make_loop_skeleton(
    smoke_recovery,
    smoke_phase,
    stall_threshold: int = 5,
) -> MilestoneLoop:
    """Build a MilestoneLoop with only the surface _maybe_recover_smoke
    touches — same skeleton pattern as test_confidence_signals.py."""
    loop = MilestoneLoop.__new__(MilestoneLoop)
    loop._smoke_recovery = smoke_recovery
    loop._smoke = smoke_phase
    loop._smoke_recovery_stall_threshold = stall_threshold
    loop._on_status = None
    loop._project_root = MagicMock()
    return loop


def _arch_with_services(names: list[str]):
    arch = MagicMock()
    arch.services = [MagicMock(name=f"svc_{n}") for n in names]
    for svc, name in zip(arch.services, names):
        svc.name = name
    return arch


def _milestone(name: str = "M1"):
    m = MagicMock()
    m.name = name
    return m


def _state_stub():
    """A MilestoneState stand-in: just records mark_phase calls."""
    s = MagicMock()
    s.marks = []
    s.mark_phase.side_effect = lambda p, d: s.marks.append((p, d))
    return s


class TestPassThroughCases:
    def test_no_smoke_recovery_returns_input(self):
        loop = _make_loop_skeleton(smoke_recovery=None, smoke_phase=MagicMock())
        result = _result(["route[/x] 500"])
        out = loop._maybe_recover_smoke(
            smoke_result=result,
            milestone=_milestone(),
            architecture=_arch_with_services(["backend"]),
            auth_contract=None,
            state=_state_stub(),
        )
        assert out is result

    def test_passing_smoke_returns_input(self):
        # If smoke already passed, recovery should not even be tried.
        sr = MagicMock()
        loop = _make_loop_skeleton(smoke_recovery=sr, smoke_phase=MagicMock())
        passing = _result([])
        out = loop._maybe_recover_smoke(
            smoke_result=passing,
            milestone=_milestone(),
            architecture=_arch_with_services(["backend"]),
            auth_contract=None,
            state=_state_stub(),
        )
        assert out is passing
        sr.recover.assert_not_called()


class TestIterativeLoop:
    def test_converges_in_one_iteration(self):
        sr = MagicMock()
        sr.recover.return_value = SmokeRecoveryResult(
            attempted=True, succeeded=True, summary="fixed",
        )
        smoke = MagicMock()
        smoke.run.return_value = _result([])  # post-recovery: all green
        loop = _make_loop_skeleton(
            smoke_recovery=sr, smoke_phase=smoke, stall_threshold=5,
        )

        out = loop._maybe_recover_smoke(
            smoke_result=_result(["route[/x] 500", "route[/y] 500"]),
            milestone=_milestone(),
            architecture=_arch_with_services(["backend"]),
            auth_contract=None,
            state=_state_stub(),
        )
        assert out.passed is True
        assert sr.recover.call_count == 1
        assert smoke.run.call_count == 1

    def test_keeps_iterating_while_failures_decreasing(self):
        # 3 failures → 2 → 1 → 0. Loop should run 3 iterations and
        # stop on convergence, NOT on the stall threshold.
        sr = MagicMock()
        sr.recover.return_value = SmokeRecoveryResult(
            attempted=True, succeeded=False, summary="partial",
        )
        smoke = MagicMock()
        smoke.run.side_effect = [
            _result(["a", "b"]),     # iter 1: 3 → 2
            _result(["a"]),          # iter 2: 2 → 1
            _result([]),             # iter 3: 1 → 0 → converged
        ]
        loop = _make_loop_skeleton(
            smoke_recovery=sr, smoke_phase=smoke, stall_threshold=2,
        )

        out = loop._maybe_recover_smoke(
            smoke_result=_result(["a", "b", "c"]),
            milestone=_milestone(),
            architecture=_arch_with_services(["backend"]),
            auth_contract=None,
            state=_state_stub(),
        )
        assert out.passed is True
        assert sr.recover.call_count == 3

    def test_stops_at_stall_threshold_when_no_progress(self):
        # Failures never decrease — stall threshold = 3, so 3 iterations
        # then halt.
        sr = MagicMock()
        sr.recover.return_value = SmokeRecoveryResult(
            attempted=True, succeeded=False, summary="stuck",
        )
        smoke = MagicMock()
        smoke.run.return_value = _result(["a", "b"])  # flat
        loop = _make_loop_skeleton(
            smoke_recovery=sr, smoke_phase=smoke, stall_threshold=3,
        )

        out = loop._maybe_recover_smoke(
            smoke_result=_result(["a", "b"]),
            milestone=_milestone(),
            architecture=_arch_with_services(["backend"]),
            auth_contract=None,
            state=_state_stub(),
        )
        assert out.passed is False
        assert sr.recover.call_count == 3

    def test_regression_counts_toward_stall(self):
        # Failures go UP → regression. With threshold=2, two regressions
        # in a row should halt.
        sr = MagicMock()
        sr.recover.return_value = SmokeRecoveryResult(
            attempted=True, succeeded=False, summary="made it worse",
        )
        smoke = MagicMock()
        smoke.run.side_effect = [
            _result(["a", "b"]),         # iter 1: 1 → 2 (regression)
            _result(["a", "b", "c"]),    # iter 2: 2 → 3 (regression → halt)
        ]
        loop = _make_loop_skeleton(
            smoke_recovery=sr, smoke_phase=smoke, stall_threshold=2,
        )

        out = loop._maybe_recover_smoke(
            smoke_result=_result(["a"]),
            milestone=_milestone(),
            architecture=_arch_with_services(["backend"]),
            auth_contract=None,
            state=_state_stub(),
        )
        assert sr.recover.call_count == 2
        assert len(out.critical_failures) == 3

    def test_progress_resets_stall_counter(self):
        # Stalled, stalled, PROGRESS, stalled, stalled, stalled → halt at
        # iter 6 (3 consecutive no-progress after the reset). With
        # threshold=3, the early stalls would have stopped at iter 3 if
        # progress didn't reset. Showing the reset works.
        sr = MagicMock()
        sr.recover.return_value = SmokeRecoveryResult(
            attempted=True, succeeded=False, summary="working on it",
        )
        smoke = MagicMock()
        smoke.run.side_effect = [
            _result(["a", "b"]),  # iter 1: 2 → 2 stalled (count=1)
            _result(["a", "b"]),  # iter 2: 2 → 2 stalled (count=2)
            _result(["a"]),       # iter 3: 2 → 1 progress (count RESET to 0)
            _result(["a"]),       # iter 4: 1 → 1 stalled (count=1)
            _result(["a"]),       # iter 5: 1 → 1 stalled (count=2)
            _result(["a"]),       # iter 6: 1 → 1 stalled (count=3 → halt)
        ]
        loop = _make_loop_skeleton(
            smoke_recovery=sr, smoke_phase=smoke, stall_threshold=3,
        )

        out = loop._maybe_recover_smoke(
            smoke_result=_result(["a", "b"]),
            milestone=_milestone(),
            architecture=_arch_with_services(["backend"]),
            auth_contract=None,
            state=_state_stub(),
        )
        # If reset wasn't working we'd see 3 iters; with reset we see 6.
        assert sr.recover.call_count == 6
        assert len(out.critical_failures) == 1


class TestDefensiveBehavior:
    def test_dispatch_exception_returns_current_state(self):
        sr = MagicMock()
        sr.recover.side_effect = RuntimeError("boom")
        smoke = MagicMock()
        loop = _make_loop_skeleton(smoke_recovery=sr, smoke_phase=smoke)
        original = _result(["a"])

        out = loop._maybe_recover_smoke(
            smoke_result=original,
            milestone=_milestone(),
            architecture=_arch_with_services(["backend"]),
            auth_contract=None,
            state=_state_stub(),
        )
        assert out is original
        smoke.run.assert_not_called()  # never re-ran smoke since recover crashed

    def test_recovery_not_attempted_short_circuits(self):
        # claude binary missing at runtime → attempted=False; loop should
        # return current state without re-running smoke.
        sr = MagicMock()
        sr.recover.return_value = SmokeRecoveryResult(
            attempted=False, succeeded=False,
            summary="claude binary missing",
        )
        smoke = MagicMock()
        loop = _make_loop_skeleton(smoke_recovery=sr, smoke_phase=smoke)
        original = _result(["a"])

        out = loop._maybe_recover_smoke(
            smoke_result=original,
            milestone=_milestone(),
            architecture=_arch_with_services(["backend"]),
            auth_contract=None,
            state=_state_stub(),
        )
        assert out is original
        smoke.run.assert_not_called()
