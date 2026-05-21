"""Tests for ReviewRepairV5Loop (2026-05-20 redesign).

v5 architecture: iter-1 review → QE one-shot tests + patches
→ write to workspace → PerMilestoneDebugger.debug_with_tests().
No ResolutionChecker, no repair dispatcher, no while loop.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.driver.review_repair_v5 import ReviewRepairV5Loop
from bizniz.engineer.types import EngineerResult, EngineerPlan
from bizniz.quality_engineer.types import (
    CoverageReport, EnrichedSpec, MissingScenario,
    QEGeneratedTest, QEWriteTestsResult, QEWritePatchesResult, QEGeneratedPatch,
)


# ── Fixtures ────────────────────────────────────────────────────────


def _milestone():
    m = MagicMock()
    m.name = "M1"
    return m


def _arch():
    return MagicMock()


def _spec():
    return EnrichedSpec(milestone_name="M1", capabilities=[])


def _engineer_result():
    return EngineerResult(
        plan=EngineerPlan(approach="v4 IMPLEMENT", issues=[]),
        summary="",
        final_test_status="passed",
        completed_issue_ids=[],
        deferred_issue_ids=[],
        completed_units=[],
        deferred_units=[],
        notes=[],
    )


def _unapproved_review():
    coverage = CoverageReport(
        milestone_name="M1",
        approved=False,
        missing_scenarios=[
            MissingScenario(
                capability_id="cap_a",
                scenario="first_login",
                priority="critical",
            ),
        ],
    )
    code_review = CodeReviewReport(milestone_name="M1", approved=True)
    return coverage, code_review


def _approved_review():
    return (
        CoverageReport(milestone_name="M1", approved=True),
        CodeReviewReport(milestone_name="M1", approved=True),
    )


def _qe_with_one_test(path="tests/test_integration.py"):
    qe = MagicMock()
    qe.write_tests.return_value = QEWriteTestsResult(
        tests=[
            QEGeneratedTest(
                path=path,
                content="def test_first_login(): pass",
                scope="integration",
                service="backend",
                finding_ids=["qe:cap_a:first_login"],
            )
        ]
    )
    qe.write_patches.return_value = QEWritePatchesResult(patches=[])
    return qe


def _make_loop(**kwargs):
    defaults = dict(
        phase_review_parallel=MagicMock(return_value=_unapproved_review()),
        qe_agent=_qe_with_one_test(),
        architecture_summary="arch summary",
        compose_path="/project/docker-compose.yml",
    )
    defaults.update(kwargs)
    return ReviewRepairV5Loop(**defaults)


def _run(loop):
    return loop.run(
        milestone=_milestone(),
        architecture=_arch(),
        spec=_spec(),
        initial_result=_engineer_result(),
        auth_contract=None,
        prior_list=[],
        milestone_index=1,
    )


# ── Early-exit: both approved ───────────────────────────────────────


class TestApprovedOnInitialReview:
    def test_returns_zero_iters_when_both_approved(self):
        qe = MagicMock()
        loop = _make_loop(
            phase_review_parallel=MagicMock(return_value=_approved_review()),
            qe_agent=qe,
        )
        cov, cr, result, iters, history = _run(loop)
        assert iters == 0
        assert cov.approved is True
        assert cr.approved is True
        qe.write_tests.assert_not_called()
        qe.write_patches.assert_not_called()

    def test_returns_original_result_when_approved(self):
        initial = _engineer_result()
        loop = _make_loop(
            phase_review_parallel=MagicMock(return_value=_approved_review()),
        )
        _, _, result, _, _ = loop.run(
            milestone=_milestone(),
            architecture=_arch(),
            spec=_spec(),
            initial_result=initial,
            auth_contract=None,
            prior_list=[],
            milestone_index=1,
        )
        assert result is initial


# ── QE one-shot test writing ─────────────────────────────────────────


class TestQEWriteTests:
    def test_calls_qe_write_tests_on_unapproved(self):
        qe = _qe_with_one_test()
        loop = _make_loop(qe_agent=qe)
        _run(loop)
        qe.write_tests.assert_called_once()

    def test_calls_qe_write_patches_after_tests(self):
        qe = _qe_with_one_test()
        loop = _make_loop(qe_agent=qe)
        _run(loop)
        qe.write_patches.assert_called_once()

    def test_write_tests_failure_skips_debugger(self):
        qe = MagicMock()
        qe.write_tests.side_effect = RuntimeError("LLM error")
        debugger = MagicMock()
        loop = _make_loop(qe_agent=qe, milestone_debugger=debugger)
        _, _, _, iters, history = _run(loop)
        assert iters == 1
        assert "qe_write_tests failed" in history
        debugger.debug_with_tests.assert_not_called()

    def test_write_patches_failure_proceeds_with_tests_only(self):
        qe = MagicMock()
        qe.write_tests.return_value = QEWriteTestsResult(
            tests=[
                QEGeneratedTest(
                    path="tests/test_x.py",
                    content="def test_x(): pass",
                    scope="unit",
                    service="backend",
                    finding_ids=[],
                )
            ]
        )
        qe.write_patches.side_effect = RuntimeError("patches failed")
        debugger = MagicMock()
        debugger.debug_with_tests.return_value = MagicMock(clean=True, files_touched=0)
        written = {}
        loop = _make_loop(
            qe_agent=qe,
            milestone_debugger=debugger,
            write_workspace_file=lambda p, c: written.update({p: c}),
        )
        _, _, _, iters, _ = _run(loop)
        # Debugger still fires — tests were written despite patch failure.
        debugger.debug_with_tests.assert_called_once()


# ── Workspace file writing ───────────────────────────────────────────


class TestWorkspaceFileWriting:
    def test_writes_generated_tests_to_workspace(self):
        written = {}

        def _write(path, content):
            written[path] = content

        qe = _qe_with_one_test("tests/test_login.py")
        loop = _make_loop(qe_agent=qe, write_workspace_file=_write)
        _run(loop)
        assert "tests/test_login.py" in written

    def test_writes_generated_patches_to_workspace(self):
        written = {}

        def _write(path, content):
            written[path] = content

        qe = MagicMock()
        qe.write_tests.return_value = QEWriteTestsResult(tests=[])
        qe.write_patches.return_value = QEWritePatchesResult(
            patches=[
                QEGeneratedPatch(
                    path="app/routes/auth.py",
                    content="def login(): pass",
                    finding_ids=["qe:cap_a:first_login"],
                )
            ]
        )
        loop = _make_loop(qe_agent=qe, write_workspace_file=_write)
        _run(loop)
        assert "app/routes/auth.py" in written

    def test_no_workspace_write_when_closure_missing(self):
        # Shouldn't raise — just logs.
        qe = _qe_with_one_test()
        loop = _make_loop(qe_agent=qe, write_workspace_file=None)
        _, _, _, iters, _ = _run(loop)
        assert iters == 1


# ── Debugger handoff ─────────────────────────────────────────────────


class TestDebuggerHandoff:
    def test_debugger_called_with_test_paths(self):
        debugger = MagicMock()
        debugger.debug_with_tests.return_value = MagicMock(clean=True, files_touched=2)
        written = {}
        qe = _qe_with_one_test("tests/test_integration.py")
        loop = _make_loop(
            qe_agent=qe,
            milestone_debugger=debugger,
            write_workspace_file=lambda p, c: written.update({p: c}),
        )
        _run(loop)
        debugger.debug_with_tests.assert_called_once()
        call_kwargs = debugger.debug_with_tests.call_args.kwargs
        assert "tests/test_integration.py" in call_kwargs["test_paths"]
        assert call_kwargs["milestone_name"] == "M1"

    def test_debugger_skipped_when_no_tests_written(self):
        debugger = MagicMock()
        qe = MagicMock()
        qe.write_tests.return_value = QEWriteTestsResult(tests=[])
        qe.write_patches.return_value = QEWritePatchesResult(patches=[])
        # write_workspace_file missing → no test paths collected
        loop = _make_loop(qe_agent=qe, milestone_debugger=debugger)
        _run(loop)
        debugger.debug_with_tests.assert_not_called()

    def test_debugger_skipped_when_not_wired(self):
        qe = _qe_with_one_test()
        written = {}
        loop = _make_loop(
            qe_agent=qe,
            milestone_debugger=None,
            write_workspace_file=lambda p, c: written.update({p: c}),
        )
        # Should not raise.
        _, _, _, iters, _ = _run(loop)
        assert iters == 1

    def test_debugger_exception_is_logged_not_raised(self):
        debugger = MagicMock()
        debugger.debug_with_tests.side_effect = RuntimeError("docker crashed")
        written = {}
        qe = _qe_with_one_test()
        loop = _make_loop(
            qe_agent=qe,
            milestone_debugger=debugger,
            write_workspace_file=lambda p, c: written.update({p: c}),
        )
        # Should not propagate.
        _, _, _, iters, _ = _run(loop)
        assert iters == 1


# ── Git snapshot ─────────────────────────────────────────────────────


class TestGitSnapshot:
    def test_snapshot_taken_before_debugger(self):
        project_git = MagicMock()
        debugger = MagicMock()
        debugger.debug_with_tests.return_value = MagicMock(clean=True, files_touched=1)
        written = {}
        qe = _qe_with_one_test()
        loop = _make_loop(
            qe_agent=qe,
            project_git=project_git,
            milestone_debugger=debugger,
            write_workspace_file=lambda p, c: written.update({p: c}),
        )
        _run(loop)
        project_git.snapshot_for_repair_iter.assert_called_once_with(
            milestone_index=1, iter_idx=1
        )

    def test_snapshot_failure_is_non_fatal(self):
        project_git = MagicMock()
        project_git.snapshot_for_repair_iter.side_effect = RuntimeError("git error")
        written = {}
        qe = _qe_with_one_test()
        loop = _make_loop(
            qe_agent=qe,
            project_git=project_git,
            write_workspace_file=lambda p, c: written.update({p: c}),
        )
        # Should not raise.
        _, _, _, iters, _ = _run(loop)
        assert iters == 1


# ── Workspace file collection ─────────────────────────────────────────


class TestWorkspaceFileCollection:
    def test_discover_closure_called_on_unapproved(self):
        discover = MagicMock(return_value=[])
        snapshot = MagicMock(return_value={})
        loop = _make_loop(
            discover_workspace_files=discover,
            snapshot_workspace_files=snapshot,
        )
        _run(loop)
        discover.assert_called_once()

    def test_source_and_test_files_separated(self):
        paths = [
            "app/routes/auth.py",
            "tests/test_auth.py",
            "app/models/user.py",
        ]
        discover = MagicMock(return_value=paths)
        snapshot_calls = []

        def _snapshot(p_list):
            snapshot_calls.append(sorted(p_list))
            return {p: "content" for p in p_list}

        loop = _make_loop(
            discover_workspace_files=discover,
            snapshot_workspace_files=_snapshot,
        )
        _run(loop)
        # Two snapshot calls: one for test files, one for source files.
        all_paths_requested = [p for batch in snapshot_calls for p in batch]
        assert "tests/test_auth.py" in all_paths_requested
        assert "app/routes/auth.py" in all_paths_requested

    def test_source_files_capped_at_40(self):
        many_source = [f"app/file_{i}.py" for i in range(100)]
        discover = MagicMock(return_value=many_source)
        snapshot_calls = []

        def _snapshot(p_list):
            snapshot_calls.append(p_list)
            return {}

        loop = _make_loop(
            discover_workspace_files=discover,
            snapshot_workspace_files=_snapshot,
        )
        _run(loop)
        # Each snapshot call should be capped at 40.
        for batch in snapshot_calls:
            assert len(batch) <= 40
