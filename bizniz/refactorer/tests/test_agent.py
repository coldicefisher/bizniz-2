"""Tests for the RefactorerAgent (Phase G)."""
from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from bizniz.refactorer.agent import (
    RefactorerAgent, RefactorerRunResult,
)
from bizniz.refactorer.anti_patterns import (
    AntiPatternFinding, AntiPatternReport,
)
from bizniz.refactorer.cpd import (
    CPDConfig, CPDReport, DuplicateBlock,
)
from bizniz.refactorer.extraction_executor import (
    ExtractionExecutor, ExtractionResult,
)
from bizniz.refactorer.extraction_planner import (
    ExtractionPlan, ExtractionPlanReport,
)
from bizniz.refactorer.why_classifier import (
    WhyClassifier, WhyReport, WhyVerdict,
)


def _plan(hash_: str = "p1", disposition: str = "extract",
          risk: float = 0.2) -> ExtractionPlan:
    return ExtractionPlan(
        duplicate_hash=hash_,
        language="python",
        services_involved=["a", "b"],
        source_files=["/proj/a/x.py", "/proj/b/x.py"],
        token_count=80,
        files_count=2,
        instance_count=2,
        suggested_core_path="shared/x.py",
        risk_score=risk,
        disposition=disposition,
    )


def _ext_result(plan: ExtractionPlan, status: str) -> ExtractionResult:
    return ExtractionResult(
        plan_hash=plan.duplicate_hash, status=status,
    )


_DEFAULT_FILES = ["/proj/a/x.py", "/proj/b/x.py"]


def _make_agent(
    *,
    files=None,    # None = default; pass [] to test empty case.
    cpd_dups: List[DuplicateBlock] = None,
    anti_findings: List[AntiPatternFinding] = None,
    plans: List[ExtractionPlan] = None,
    executor_responses: List[ExtractionResult] = None,
    why_classifier=None,
    consecutive_failures_cap: int = 3,
    max_extractions: int = 50,
) -> tuple:
    """Build a RefactorerAgent with everything injectable mocked."""
    if files is None:
        files = _DEFAULT_FILES
    cpd_dups = cpd_dups or []
    anti_findings = anti_findings or []
    plans = plans or []
    executor_responses = executor_responses or []

    executor = MagicMock(spec=ExtractionExecutor)
    response_iter = iter(executor_responses)
    def _exec(plan):
        try:
            return next(response_iter)
        except StopIteration:
            return _ext_result(plan, "failed")
    executor.execute.side_effect = _exec

    agent = RefactorerAgent(
        project_root=Path("/proj"),
        executor=executor,
        why_classifier=why_classifier,
        walk_fn=lambda root: files,
        cpd_fn=lambda fs, **kw: CPDReport(
            config=CPDConfig(), duplicates=cpd_dups,
        ),
        scan_fn=lambda fs: AntiPatternReport(
            findings=anti_findings, files_scanned=len(fs),
        ),
        plan_fn=lambda cpd, **kw: ExtractionPlanReport(plans=plans),
        consecutive_failures_cap=consecutive_failures_cap,
        max_extractions=max_extractions,
    )
    return agent, executor


# ── Skipping paths ───────────────────────────────────────────────


class TestSkippingPaths:
    def test_no_files_short_circuits(self):
        agent, executor = _make_agent(files=[])
        result = agent.run()
        assert result.skipped_reason == "no source files found"
        executor.execute.assert_not_called()

    def test_no_extract_plans_returns_clean(self):
        agent, executor = _make_agent()
        result = agent.run()
        assert result.skipped_reason is None
        assert result.extractions_applied == 0
        executor.execute.assert_not_called()


# ── Happy path ───────────────────────────────────────────────────


class TestHappyPath:
    def test_single_extraction_applied(self):
        plan = _plan()
        agent, executor = _make_agent(
            plans=[plan],
            executor_responses=[_ext_result(plan, "applied")],
        )
        result = agent.run()
        assert result.passed is True
        assert result.extractions_applied == 1
        assert result.extractions_reverted == 0

    def test_multiple_extractions_all_applied(self):
        plans = [_plan(f"p{i}") for i in range(3)]
        agent, executor = _make_agent(
            plans=plans,
            executor_responses=[
                _ext_result(p, "applied") for p in plans
            ],
        )
        result = agent.run()
        assert result.extractions_applied == 3


# ── Failure paths ────────────────────────────────────────────────


class TestFailurePaths:
    def test_revert_counted_separately(self):
        plans = [_plan(f"p{i}") for i in range(3)]
        agent, executor = _make_agent(
            plans=plans,
            executor_responses=[
                _ext_result(plans[0], "applied"),
                _ext_result(plans[1], "reverted"),
                _ext_result(plans[2], "applied"),
            ],
        )
        result = agent.run()
        assert result.extractions_applied == 2
        assert result.extractions_reverted == 1

    def test_no_changes_counted_separately(self):
        plans = [_plan(f"p{i}") for i in range(2)]
        agent, executor = _make_agent(
            plans=plans,
            executor_responses=[
                _ext_result(plans[0], "no_changes"),
                _ext_result(plans[1], "applied"),
            ],
        )
        result = agent.run()
        assert result.extractions_applied == 1
        assert result.extractions_skipped == 1

    def test_consecutive_failures_cap_stops_early(self):
        plans = [_plan(f"p{i}") for i in range(10)]
        # All fail in a row.
        agent, executor = _make_agent(
            plans=plans,
            executor_responses=[
                _ext_result(p, "failed") for p in plans
            ],
            consecutive_failures_cap=2,
        )
        result = agent.run()
        # Should have stopped after 2 consecutive failures.
        assert executor.execute.call_count == 2
        assert any(
            "consecutive failures cap" in n for n in result.notes
        )

    def test_max_extractions_cap_stops_early(self):
        plans = [_plan(f"p{i}") for i in range(20)]
        agent, executor = _make_agent(
            plans=plans,
            executor_responses=[
                _ext_result(p, "applied") for p in plans
            ],
            max_extractions=5,
        )
        result = agent.run()
        assert executor.execute.call_count == 5
        assert any(
            "max_extractions cap" in n for n in result.notes
        )

    def test_revert_does_not_break_streak(self):
        # Streak: applied → reverted → applied → applied.
        # Reverts reset the "consecutive failures" counter? They count
        # against the streak since the LLM made a wrong call. Verify
        # the implementation matches docstring.
        plans = [_plan(f"p{i}") for i in range(4)]
        agent, executor = _make_agent(
            plans=plans,
            executor_responses=[
                _ext_result(plans[0], "applied"),
                _ext_result(plans[1], "reverted"),
                _ext_result(plans[2], "applied"),
                _ext_result(plans[3], "applied"),
            ],
            consecutive_failures_cap=2,
        )
        result = agent.run()
        # Should make all 4 calls — only 1 revert, never 2 in a row.
        assert executor.execute.call_count == 4
        assert result.extractions_applied == 3


# ── Anti-pattern classifier integration ──────────────────────────


class TestAntiPatternClassifier:
    def test_classifier_called_when_findings_present(self):
        finding = AntiPatternFinding(
            pattern="drop_all_in_test", severity="critical",
            path="/proj/a/conftest.py", line=10,
            snippet="drop_all", description="d",
        )
        classifier = MagicMock(spec=WhyClassifier)
        classifier.classify_all.return_value = WhyReport(verdicts=[
            WhyVerdict(
                finding=finding,
                hypothesis="test isolation",
                confidence=0.9,
                recommended_action="rewrite",
            ),
        ])
        agent, executor = _make_agent(
            anti_findings=[finding],
            why_classifier=classifier,
        )
        result = agent.run()
        classifier.classify_all.assert_called_once()
        assert result.why_report is not None
        # Auto-fix candidate surfaced (not auto-applied yet).
        assert any(
            "auto-fix candidate" in n for n in result.notes
        )

    def test_no_classifier_skips_phase_d(self):
        finding = AntiPatternFinding(
            pattern="bare_except", severity="critical",
            path="/proj/a/x.py", line=1,
            snippet="except:", description="d",
        )
        agent, executor = _make_agent(
            anti_findings=[finding],
            why_classifier=None,
        )
        result = agent.run()
        assert result.why_report is None


# ── Aborting on unexpected exception ─────────────────────────────


class TestAborting:
    def test_unexpected_exception_caught_as_skipped(self):
        executor = MagicMock(spec=ExtractionExecutor)
        agent = RefactorerAgent(
            project_root=Path("/proj"),
            executor=executor,
            walk_fn=lambda root: ["/proj/a/x.py"],
            cpd_fn=lambda fs, **kw: (_ for _ in ()).throw(
                RuntimeError("CPD blew up"),
            ),
            scan_fn=lambda fs: AntiPatternReport(),
            plan_fn=lambda cpd, **kw: ExtractionPlanReport(),
        )
        result = agent.run()
        assert result.skipped_reason is not None
        assert "CPD blew up" in result.skipped_reason
        assert result.passed is False


# ── Status logging ───────────────────────────────────────────────


class TestStatusLogging:
    def test_status_callback_emits_progress(self):
        statuses: List[str] = []
        plan = _plan()
        agent = RefactorerAgent(
            project_root=Path("/proj"),
            executor=MagicMock(spec=ExtractionExecutor,
                                execute=MagicMock(return_value=_ext_result(plan, "applied"))),
            walk_fn=lambda root: ["/proj/a/x.py"],
            cpd_fn=lambda fs, **kw: CPDReport(config=CPDConfig()),
            scan_fn=lambda fs: AntiPatternReport(),
            plan_fn=lambda cpd, **kw: ExtractionPlanReport(plans=[plan]),
            on_status=lambda m: statuses.append(m),
        )
        agent.run()
        joined = " ".join(statuses)
        assert "walking" in joined
        assert "CPD" in joined
        assert "done in" in joined
