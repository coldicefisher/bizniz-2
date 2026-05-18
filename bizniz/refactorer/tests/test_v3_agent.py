"""Tests for the v3 refactorer orchestrator (D19 final piece).

All collaborators are stubbed — we exercise the conductor logic:
candidate unification, gate routing, plan dispatch, executor
dispatch, import-verifier check, trail bookkeeping, defensive
isolation per candidate.
"""
from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from bizniz.refactorer.anti_patterns import (
    AntiPatternFinding,
    AntiPatternReport,
)
from bizniz.refactorer.cpd import CPDConfig, CPDReport
from bizniz.refactorer.decision_gate import GateDecision
from bizniz.refactorer.destination_planner import DestinationPlan
from bizniz.refactorer.extraction_executor import ExtractionResult
from bizniz.refactorer.extraction_planner import (
    ExtractionPlan,
    ExtractionPlanReport,
)
from bizniz.refactorer.import_verifier import (
    ImportProblem,
    ImportVerifierReport,
)
from bizniz.refactorer.misplacement_scanner import (
    MisplacedLogicCandidate,
    MisplacementReport,
)
from bizniz.refactorer.v3_agent import (
    V3RefactorerAgent,
    V3RefactorerRunResult,
)


# ── Helpers ──────────────────────────────────────────────────────


def _dest_plan(path: str = "core/python/x/y.py") -> DestinationPlan:
    return DestinationPlan(
        destination_path=path,
        destination_kind="new",
        functions_to_move=["f"],
        consumer_import=f"from python_core.x.y import f",
        rationale="ok",
    )


def _ext_result(status: str = "applied") -> ExtractionResult:
    return ExtractionResult(status=status, plan_hash="test-hash")


def _empty_anti() -> AntiPatternReport:
    return AntiPatternReport(findings=[])


def _empty_cpd() -> CPDReport:
    return CPDReport(config=CPDConfig())


def _empty_misplacement() -> MisplacementReport:
    return MisplacementReport()


def _empty_plan_report() -> ExtractionPlanReport:
    return ExtractionPlanReport()


def _make_agent(
    tmp_path: Path,
    *,
    gate=None,
    dest_planner=None,
    executor=None,
    misplacement=None,
    verifier=None,
    walk=None,
    cpd_fn=None,
    anti_fn=None,
    plan_fn=None,
) -> V3RefactorerAgent:
    """Build a V3RefactorerAgent with all collaborators stubbed."""
    return V3RefactorerAgent(
        project_root=tmp_path,
        executor=executor or MagicMock(),
        decision_gate=gate or MagicMock(),
        destination_planner=dest_planner or MagicMock(),
        misplacement_scanner=misplacement or MagicMock(),
        import_verifier=verifier or MagicMock(),
        walk_fn=walk or (lambda root: ["one.py"]),
        cpd_fn=cpd_fn or (lambda files, config=None: _empty_cpd()),
        anti_patterns_fn=anti_fn or (lambda files: _empty_anti()),
        deterministic_plan_fn=plan_fn or (
            lambda report, project_root=None: _empty_plan_report()
        ),
    )


# ── Happy path ───────────────────────────────────────────────────


class TestHappyPath:
    def test_no_candidates_returns_clean(self, tmp_path):
        misplacement = MagicMock()
        misplacement.scan.return_value = _empty_misplacement()
        agent = _make_agent(tmp_path, misplacement=misplacement)
        result = agent.run()
        assert result.passed
        assert result.candidates_total == 0
        assert result.candidates_applied == 0

    def test_no_source_files_short_circuits(self, tmp_path):
        agent = _make_agent(tmp_path, walk=lambda root: [])
        result = agent.run()
        # Empty source-tree is a clean no-op, but ``passed`` is False
        # because skipped_reason is set — distinguishes "ran zero
        # candidates" from "completed a real cycle." The milestone
        # loop tolerates either.
        assert not result.passed
        assert result.skipped_reason == "no Python source files found"


# ── Candidate routing ────────────────────────────────────────────


class TestCandidateRouting:
    def test_anti_pattern_finding_flows_through_gate(self, tmp_path):
        anti = AntiPatternReport(findings=[
            AntiPatternFinding(
                pattern="hardcoded_url", path="app/foo.py", line=10,
                severity="warning", snippet="url='http://x'",
                description="hardcoded URL detected",
            ),
        ])
        gate = MagicMock()
        gate.decide.return_value = GateDecision(
            refactor=False, rationale="boilerplate", confidence=0.9,
        )
        misplacement = MagicMock()
        misplacement.scan.return_value = _empty_misplacement()
        agent = _make_agent(
            tmp_path, gate=gate, misplacement=misplacement,
            anti_fn=lambda files: anti,
        )
        result = agent.run()
        assert result.candidates_total == 1
        assert result.candidates_skipped_by_gate == 1
        # The gate saw the anti-pattern context.
        ctx = gate.decide.call_args.args[0]
        assert ctx.kind == "anti_pattern"
        assert "hardcoded_url" in ctx.summary

    def test_misplacement_candidate_flows_through(self, tmp_path):
        misplaced = MisplacedLogicCandidate(
            file_path="app/api/routes/x.py",
            function_name="create",
            line_range=(10, 20),
            why="business logic in route",
            suggested_core_module="core/python/x.py",
        )
        misplacement = MagicMock()
        misplacement.scan.return_value = MisplacementReport(
            candidates=[misplaced],
        )
        gate = MagicMock()
        gate.decide.return_value = GateDecision(
            refactor=False, rationale="skip", confidence=0.5,
        )
        agent = _make_agent(
            tmp_path, gate=gate, misplacement=misplacement,
        )
        result = agent.run()
        assert result.candidates_total == 1
        ctx = gate.decide.call_args.args[0]
        assert ctx.kind == "misplaced_logic"
        assert ctx.line_range == (10, 20)
        assert ctx.extra["function_name"] == "create"

    def test_gate_yes_proceeds_to_destination_planner(self, tmp_path):
        misplaced = MisplacedLogicCandidate(
            file_path="app/api/routes/x.py",
            function_name="create", line_range=(10, 20),
            why="real domain logic",
            suggested_core_module="core/python/x.py",
        )
        misplacement = MagicMock()
        misplacement.scan.return_value = MisplacementReport(
            candidates=[misplaced],
        )
        gate = MagicMock()
        gate.decide.return_value = GateDecision(
            refactor=True, rationale="yes", confidence=0.8,
        )
        dest_planner = MagicMock()
        dest_planner.plan_for.return_value = _dest_plan()
        executor = MagicMock()
        executor.execute.return_value = _ext_result("applied")
        verifier = MagicMock()
        verifier.verify_files.return_value = ImportVerifierReport()

        agent = _make_agent(
            tmp_path,
            gate=gate,
            dest_planner=dest_planner,
            executor=executor,
            verifier=verifier,
            misplacement=misplacement,
        )
        result = agent.run()

        dest_planner.plan_for.assert_called_once()
        executor.execute.assert_called_once()
        verifier.verify_files.assert_called_once()
        assert result.candidates_applied == 1


# ── Verify step ──────────────────────────────────────────────────


class TestVerify:
    def _setup_one_applied_candidate(self, tmp_path, verifier_report):
        misplaced = MisplacedLogicCandidate(
            file_path="app/x.py", function_name="f",
            line_range=(1, 5), why="x",
            suggested_core_module="core/python/x.py",
        )
        misplacement = MagicMock()
        misplacement.scan.return_value = MisplacementReport(
            candidates=[misplaced],
        )
        gate = MagicMock()
        gate.decide.return_value = GateDecision(
            refactor=True, rationale="y", confidence=0.9,
        )
        dest_planner = MagicMock()
        dest_planner.plan_for.return_value = _dest_plan()
        executor = MagicMock()
        executor.execute.return_value = _ext_result("applied")
        verifier = MagicMock()
        verifier.verify_files.return_value = verifier_report
        return _make_agent(
            tmp_path, gate=gate, dest_planner=dest_planner,
            executor=executor, verifier=verifier,
            misplacement=misplacement,
        )

    def test_clean_imports_counts_as_applied(self, tmp_path):
        agent = self._setup_one_applied_candidate(
            tmp_path, ImportVerifierReport(),
        )
        result = agent.run()
        assert result.candidates_applied == 1
        assert result.candidates_failed == 0
        assert result.trails[0].final_status == "applied"

    def test_import_problems_mark_failed(self, tmp_path):
        bad_report = ImportVerifierReport(problems=[
            ImportProblem(
                file_path="app/x.py", line=4,
                statement="from python_core.gone import g",
                reason="module not found",
            ),
        ])
        agent = self._setup_one_applied_candidate(tmp_path, bad_report)
        result = agent.run()
        assert result.candidates_failed == 1
        assert result.candidates_applied == 0
        trail = result.trails[0]
        assert trail.final_status == "import_check_failed"
        assert any("module not found" in p for p in trail.import_problems)


# ── Defensive ────────────────────────────────────────────────────


class TestDefensive:
    def test_one_candidate_crash_doesnt_halt_others(self, tmp_path):
        misplaced = [
            MisplacedLogicCandidate(
                file_path=f"app/{i}.py", function_name=f"f{i}",
                line_range=(1, 2), why=f"why{i}",
                suggested_core_module=f"core/python/{i}.py",
            )
            for i in range(3)
        ]
        misplacement = MagicMock()
        misplacement.scan.return_value = MisplacementReport(
            candidates=misplaced,
        )

        # First candidate's gate decision crashes. Other two succeed
        # but skip.
        gate = MagicMock()
        gate.decide.side_effect = [
            RuntimeError("first one died"),
            GateDecision(refactor=False, rationale="skip"),
            GateDecision(refactor=False, rationale="skip"),
        ]
        agent = _make_agent(
            tmp_path, gate=gate, misplacement=misplacement,
        )
        result = agent.run()
        # All three are tracked; first errored, other two skipped by gate.
        assert result.candidates_total == 3
        assert result.candidates_failed == 1
        assert result.candidates_skipped_by_gate == 2
        assert result.trails[0].final_status == "errored"
        assert "first one died" in result.trails[0].failure_reason

    def test_run_outer_exception_yields_skipped_reason(self, tmp_path):
        # Misplacement scanner blows up.
        misplacement = MagicMock()
        misplacement.scan.side_effect = RuntimeError("scanner died")
        agent = _make_agent(tmp_path, misplacement=misplacement)
        result = agent.run()
        assert not result.passed
        assert "scanner died" in result.skipped_reason

    def test_max_candidates_cap_applied(self, tmp_path):
        # 100 candidates from misplacement, cap=5.
        misplaced = [
            MisplacedLogicCandidate(
                file_path=f"app/{i}.py", function_name="f",
                line_range=(1, 2), why="w",
                suggested_core_module="core/python/x.py",
            )
            for i in range(100)
        ]
        misplacement = MagicMock()
        misplacement.scan.return_value = MisplacementReport(
            candidates=misplaced,
        )
        gate = MagicMock()
        gate.decide.return_value = GateDecision(
            refactor=False, rationale="skip",
        )
        agent = V3RefactorerAgent(
            project_root=tmp_path,
            executor=MagicMock(),
            decision_gate=gate,
            destination_planner=MagicMock(),
            misplacement_scanner=misplacement,
            import_verifier=MagicMock(),
            walk_fn=lambda root: ["one.py"],
            cpd_fn=lambda files, config=None: _empty_cpd(),
            anti_patterns_fn=lambda files: _empty_anti(),
            deterministic_plan_fn=(
                lambda report, project_root=None: _empty_plan_report()
            ),
            max_candidates=5,
        )
        result = agent.run()
        assert result.candidates_total == 5
        assert gate.decide.call_count == 5


# ── Walker default ──────────────────────────────────────────────


class TestDefaultWalker:
    def test_default_walker_excludes_tests_and_state(self, tmp_path):
        from bizniz.refactorer.v3_agent import _default_walk_python

        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "main.py").write_text("")
        (tmp_path / "app" / "tests").mkdir()
        (tmp_path / "app" / "tests" / "test_thing.py").write_text("")
        (tmp_path / ".bizniz").mkdir()
        (tmp_path / ".bizniz" / "state.py").write_text("")
        (tmp_path / "core" / "typescript" / "x.ts.py").parent.mkdir(parents=True)
        (tmp_path / "core" / "typescript" / "x.ts.py").write_text("")  # quirky
        (tmp_path / "test_root.py").write_text("")
        (tmp_path / "real.py").write_text("")

        files = _default_walk_python(tmp_path)
        rels = sorted(str(Path(f).relative_to(tmp_path)) for f in files)
        assert "app/main.py" in rels
        assert "real.py" in rels
        assert not any("tests" in r for r in rels)
        assert not any(".bizniz" in r for r in rels)
        assert not any("core/typescript" in r for r in rels)
        assert not any(r.startswith("test_") for r in rels)
