"""Tests for ``ReviewUnitOrchestrator`` and ``ReviewUnitLoop``."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bizniz.code_reviewer.types import (
    CodeReviewReport, FlaggedSymbol,
)
from bizniz.quality_engineer.types import CoverageReport
from bizniz.review_unit.batch_fix_debugger import BatchFixResult
from bizniz.review_unit.loop import ReviewUnitLoop
from bizniz.review_unit.orchestrator import ReviewUnitOrchestrator
from bizniz.review_unit.types import FindingsReport, UnifiedFinding


# ── Orchestrator ─────────────────────────────────────────────────


class TestOrchestrator:
    def test_clean_qe_clean_cr_yields_empty_report(self):
        qe = CoverageReport(milestone_name="M1", approved=True)
        cr = CodeReviewReport(milestone_name="M1", approved=True)
        orch = ReviewUnitOrchestrator(
            qe_review=lambda: qe, cr_review=lambda: cr,
        )
        report = orch.run(iteration=0)
        assert report.count == 0
        assert report.iteration == 0

    def test_aggregates_qe_and_cr_findings(self):
        qe = CoverageReport(
            milestone_name="M1", approved=False,
            coverage_by_capability={"cap.foo": "missing"},
        )
        cr = CodeReviewReport(
            milestone_name="M1", approved=False,
            flagged_symbols=[FlaggedSymbol(
                file="a.py", symbol="x", kind="import",
                reason="r", severity="critical",
            )],
        )
        orch = ReviewUnitOrchestrator(
            qe_review=lambda: qe, cr_review=lambda: cr,
        )
        report = orch.run()
        sources = {f.source for f in report.findings}
        assert sources == {"quality_engineer", "code_reviewer"}
        assert report.count == 2

    def test_qe_failure_records_finding_but_cr_still_runs(self):
        cr = CodeReviewReport(
            milestone_name="M1", approved=False,
            flagged_symbols=[FlaggedSymbol(
                file="a.py", symbol="x", kind="import",
                reason="r", severity="critical",
            )],
        )
        def boom_qe():
            raise RuntimeError("QE went sideways")
        orch = ReviewUnitOrchestrator(
            qe_review=boom_qe, cr_review=lambda: cr,
        )
        report = orch.run()
        # CR finding present + a source_error finding for QE.
        fingerprints = {f.fingerprint for f in report.findings}
        assert any("source_error" in fp for fp in fingerprints)
        assert any("cr.symbol" in fp for fp in fingerprints)
        assert report.count == 2


# ── Loop ─────────────────────────────────────────────────────────


def _stub_debugger() -> MagicMock:
    """Stub debugger that records calls but applies no real fixes."""
    d = MagicMock()
    d.run.return_value = BatchFixResult(
        summary="stub fix",
        fixes_applied=[],
        skipped_fingerprints=[],
        wall_s=0.1,
    )
    return d


class TestLoop:
    def test_clean_first_iteration_approves_immediately(self, tmp_path):
        # Orchestrator returns an empty report — no fixes needed.
        orch = MagicMock()
        orch.run.return_value = FindingsReport(iteration=0, findings=[])
        debugger = _stub_debugger()
        loop = ReviewUnitLoop(
            orchestrator=orch,
            debugger_factory=lambda: debugger,
            workspace_root=tmp_path,
        )
        result = loop.run()
        assert result.approved is True
        assert result.iterations == 1
        assert result.final_findings.count == 0
        # Clean on first iter — debugger never called.
        debugger.run.assert_not_called()

    def test_progress_then_clean_converges(self, tmp_path):
        # Iter 0: 3 findings. Iter 1: 1 finding. Iter 2: 0 findings.
        sequence = [
            FindingsReport(iteration=0, findings=[
                UnifiedFinding(source="quality_engineer", severity="high",
                               fingerprint=f"f{i}", message=f"m{i}")
                for i in range(3)
            ]),
            FindingsReport(iteration=1, findings=[
                UnifiedFinding(source="code_reviewer", severity="medium",
                               fingerprint="leftover", message="last one"),
            ]),
            FindingsReport(iteration=2, findings=[]),
        ]
        orch = MagicMock()
        orch.run.side_effect = sequence
        debugger = _stub_debugger()
        loop = ReviewUnitLoop(
            orchestrator=orch,
            debugger_factory=lambda: debugger,
            workspace_root=tmp_path,
        )
        result = loop.run()
        assert result.approved is True
        # Three orchestrator runs total (0, 1, 2). Debugger called twice
        # (between iter 0→1 and 1→2; iter 2 was clean → no debugger call).
        assert orch.run.call_count == 3
        assert debugger.run.call_count == 2
        # Iter 0 has no prior → "initial"; iter 1 vs iter 0 was a drop
        # (3→1) → "progress"; iter 2 hit zero → "clean".
        assert [v.verdict for v in result.history] == ["initial", "progress", "clean"]

    def test_stall_threshold_halts_loop(self, tmp_path):
        # Findings count never drops — should bail at stall_threshold.
        stuck = [
            FindingsReport(iteration=i, findings=[
                UnifiedFinding(source="quality_engineer", severity="high",
                               fingerprint=f"f{i}", message="stuck"),
            ])
            for i in range(10)
        ]
        orch = MagicMock()
        orch.run.side_effect = stuck
        debugger = _stub_debugger()
        loop = ReviewUnitLoop(
            orchestrator=orch,
            debugger_factory=lambda: debugger,
            workspace_root=tmp_path,
            stall_threshold=3,
            hard_cap=20,
        )
        result = loop.run()
        assert result.approved is False
        assert "stall_threshold" in result.halt_reason

    def test_hard_cap_halts_loop(self, tmp_path):
        # Findings drop each iteration but the cap is too tight.
        reports = [
            FindingsReport(iteration=i, findings=[
                UnifiedFinding(source="quality_engineer", severity="medium",
                               fingerprint=f"f{j}", message="m")
                for j in range(10 - i)  # 10, 9, 8...
            ])
            for i in range(20)
        ]
        orch = MagicMock()
        orch.run.side_effect = reports
        debugger = _stub_debugger()
        loop = ReviewUnitLoop(
            orchestrator=orch,
            debugger_factory=lambda: debugger,
            workspace_root=tmp_path,
            hard_cap=3,
            stall_threshold=10,
        )
        result = loop.run()
        assert result.approved is False
        assert "hard_cap" in result.halt_reason

    def test_orchestrator_exception_halts_with_reason(self, tmp_path):
        orch = MagicMock()
        orch.run.side_effect = RuntimeError("orchestrator blew up")
        debugger = _stub_debugger()
        loop = ReviewUnitLoop(
            orchestrator=orch,
            debugger_factory=lambda: debugger,
            workspace_root=tmp_path,
        )
        result = loop.run()
        assert result.approved is False
        assert "orchestrator_error" in result.halt_reason
