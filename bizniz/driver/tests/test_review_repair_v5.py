"""Tests for the v5 review/repair loop wiring.

Mocks the resolution checkers + dispatcher + project_git so we
exercise the loop's control flow without touching real LLMs or git.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bizniz.canonical_findings.types import (
    CanonicalFinding, CanonicalReport,
    FindingResolution, ResolutionReport,
)
from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.driver.review_repair_v5 import ReviewRepairV5Loop
from bizniz.engineer.types import EngineerResult, EngineerPlan
from bizniz.quality_engineer.types import (
    CoverageReport, EnrichedSpec, MissingScenario,
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


def _initial_review_unapproved():
    """Returns a (CoverageReport, CodeReviewReport) pair that's
    NOT approved (forces the loop into iter 2+)."""
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


def _initial_review_approved():
    coverage = CoverageReport(milestone_name="M1", approved=True)
    code_review = CodeReviewReport(milestone_name="M1", approved=True)
    return coverage, code_review


# ── Loop tests ──────────────────────────────────────────────────────


class TestApprovedOnInitialReview:
    def test_zero_iter_when_initial_approves(self):
        review = MagicMock(return_value=_initial_review_approved())
        dispatcher = MagicMock()
        loop = ReviewRepairV5Loop(
            phase_review_parallel=review,
            repair_dispatcher=dispatcher,
            qe_resolution_checker=MagicMock(),
            cr_resolution_checker=MagicMock(),
        )
        cov, cr, result, iters, history = loop.run(
            milestone=_milestone(),
            architecture=_arch(),
            spec=_spec(),
            initial_result=_engineer_result(),
            auth_contract=None,
            prior_list=[],
            milestone_index=1,
        )
        assert iters == 0
        assert cov.approved is True
        assert cr.approved is True
        dispatcher.repair.assert_not_called()


class TestConvergence:
    def test_one_iter_resolves_finding_then_approved(self):
        review = MagicMock(return_value=_initial_review_unapproved())
        dispatcher = MagicMock()
        dispatcher.repair.return_value = _engineer_result()

        # Resolution checker: after iter 1's repair, says everything resolved.
        def _check_both(*, qe_checker, cr_checker, canonical, iter_idx, current_files, on_status=None):
            return ResolutionReport(
                milestone_name="M1", iter_idx=iter_idx,
                resolutions=[
                    FindingResolution(
                        finding_id=f.id, status="resolved",
                        evidence="fixed",
                    )
                    for f in canonical.findings
                ],
            )

        from unittest.mock import patch
        with patch(
            "bizniz.driver.review_repair_v5.check_both_sources_parallel",
            side_effect=_check_both,
        ):
            loop = ReviewRepairV5Loop(
                phase_review_parallel=review,
                repair_dispatcher=dispatcher,
                qe_resolution_checker=MagicMock(),
                cr_resolution_checker=MagicMock(),
                stall_threshold=3,
                hard_cap=10,
            )
            cov, cr, _r, iters, history = loop.run(
                milestone=_milestone(),
                architecture=_arch(),
                spec=_spec(),
                initial_result=_engineer_result(),
                auth_contract=None,
                prior_list=[],
                milestone_index=1,
            )
        assert iters == 1
        assert cov.approved is True
        dispatcher.repair.assert_called_once()

    def test_stall_halts_when_no_progress(self):
        review = MagicMock(return_value=_initial_review_unapproved())
        dispatcher = MagicMock()
        dispatcher.repair.return_value = _engineer_result()

        # Resolution checker keeps saying still_present.
        def _check_both(*, qe_checker, cr_checker, canonical, iter_idx, current_files, on_status=None):
            return ResolutionReport(
                milestone_name="M1", iter_idx=iter_idx,
                resolutions=[
                    FindingResolution(
                        finding_id=f.id, status="still_present",
                        evidence="not fixed",
                    )
                    for f in canonical.findings
                ],
            )

        from unittest.mock import patch
        with patch(
            "bizniz.driver.review_repair_v5.check_both_sources_parallel",
            side_effect=_check_both,
        ):
            loop = ReviewRepairV5Loop(
                phase_review_parallel=review,
                repair_dispatcher=dispatcher,
                qe_resolution_checker=MagicMock(),
                cr_resolution_checker=MagicMock(),
                stall_threshold=2,
                hard_cap=10,
            )
            cov, cr, _r, iters, history = loop.run(
                milestone=_milestone(),
                architecture=_arch(),
                spec=_spec(),
                initial_result=_engineer_result(),
                auth_contract=None,
                prior_list=[],
                milestone_index=1,
            )
        # Halts at stall_threshold (2) iters of no progress.
        assert iters == 2
        # Coverage report synthesized with approved=False.
        assert cov.approved is False


class TestRegressionRollback:
    def test_regression_triggers_git_rollback(self):
        """When the resolution check flips a finding from resolved →
        still_present (regression), the loop calls ProjectGit.
        rollback_repair_iter to undo this iter's repair."""
        # Iter 1 returns review with 2 findings.
        coverage = CoverageReport(
            milestone_name="M1", approved=False,
            missing_scenarios=[
                MissingScenario(capability_id="c1", scenario="s1", priority="critical"),
                MissingScenario(capability_id="c2", scenario="s2", priority="critical"),
            ],
        )
        code_review = CodeReviewReport(milestone_name="M1", approved=True)
        review = MagicMock(return_value=(coverage, code_review))
        dispatcher = MagicMock()
        dispatcher.repair.return_value = _engineer_result()

        project_git = MagicMock()
        project_git.snapshot_for_repair_iter.return_value = "m1-repair1-pre"

        # Iter 1 review: f1 resolved, f2 still_present (progress).
        # Iter 2 review: f1 still_present (REGRESSION), f2 resolved.
        check_calls = [0]
        def _check_both(*, qe_checker, cr_checker, canonical, iter_idx, current_files, on_status=None):
            ids = [f.id for f in canonical.findings]
            check_calls[0] += 1
            if check_calls[0] == 1:
                # Iter 1 result.
                return ResolutionReport(
                    milestone_name="M1", iter_idx=iter_idx,
                    resolutions=[
                        FindingResolution(finding_id=ids[0], status="resolved", evidence=""),
                        FindingResolution(finding_id=ids[1], status="still_present", evidence=""),
                    ],
                )
            # Iter 2 result — f1 regressed!
            return ResolutionReport(
                milestone_name="M1", iter_idx=iter_idx,
                resolutions=[
                    FindingResolution(finding_id=ids[0], status="still_present", evidence="regressed!"),
                    FindingResolution(finding_id=ids[1], status="resolved", evidence=""),
                ],
            )

        from unittest.mock import patch
        with patch(
            "bizniz.driver.review_repair_v5.check_both_sources_parallel",
            side_effect=_check_both,
        ):
            loop = ReviewRepairV5Loop(
                phase_review_parallel=review,
                repair_dispatcher=dispatcher,
                qe_resolution_checker=MagicMock(),
                cr_resolution_checker=MagicMock(),
                project_git=project_git,
                stall_threshold=5,
                hard_cap=4,
            )
            cov, cr, _r, iters, history = loop.run(
                milestone=_milestone(),
                architecture=_arch(),
                spec=_spec(),
                initial_result=_engineer_result(),
                auth_contract=None,
                prior_list=[],
                milestone_index=1,
            )
        # Rollback was invoked.
        assert project_git.rollback_repair_iter.called
        # Snapshot was taken at least once before each repair iter.
        assert project_git.snapshot_for_repair_iter.call_count >= 1


class TestCollectFilesForCheck:
    """2026-05-20 regression: the prior collector only included
    paths with a ``file_hint``. QE coverage findings (the bulk of
    findings) reference a ``capability_id``, NOT a file — so the
    checker got zero files and judged every finding still_present.

    The fix wires a ``discover_workspace_files`` closure so the
    checker sees the actual workspace code/tests."""

    def _canonical_with_no_file_hints(self):
        return CanonicalReport(
            milestone_name="M1",
            iter_frozen=1,
            findings=[
                CanonicalFinding(
                    id="qe:cap_a:first_login",
                    source="quality_engineer",
                    priority="critical",
                    summary="missing scenario: first_login",
                    capability_id="cap_a",
                    file_hint=None,
                    status="initial",
                ),
            ],
        )

    def test_includes_discovered_files_when_no_file_hints(self):
        snapshot = MagicMock(
            return_value={"app/api/routes/me.py": "def me(): ..."}
        )
        discover = MagicMock(
            return_value=["app/api/routes/me.py", "tests/test_me.py"]
        )
        loop = ReviewRepairV5Loop(
            phase_review_parallel=MagicMock(),
            repair_dispatcher=MagicMock(),
            qe_resolution_checker=MagicMock(),
            cr_resolution_checker=MagicMock(),
            snapshot_workspace_files=snapshot,
            discover_workspace_files=discover,
        )
        files = loop._collect_files_for_check(
            self._canonical_with_no_file_hints()
        )
        discover.assert_called_once()
        # snapshot was asked for the discovered paths, sorted.
        called_paths = snapshot.call_args.args[0]
        assert "app/api/routes/me.py" in called_paths
        assert "tests/test_me.py" in called_paths
        # Returned content threads through.
        assert "app/api/routes/me.py" in files

    def test_caps_at_60_paths(self):
        many = [f"app/file_{i}.py" for i in range(200)]
        snapshot = MagicMock(return_value={})
        discover = MagicMock(return_value=many)
        loop = ReviewRepairV5Loop(
            phase_review_parallel=MagicMock(),
            repair_dispatcher=MagicMock(),
            qe_resolution_checker=MagicMock(),
            cr_resolution_checker=MagicMock(),
            snapshot_workspace_files=snapshot,
            discover_workspace_files=discover,
        )
        loop._collect_files_for_check(
            self._canonical_with_no_file_hints()
        )
        called_paths = snapshot.call_args.args[0]
        assert len(called_paths) == 60

    def test_empty_when_no_snapshot_closure(self):
        loop = ReviewRepairV5Loop(
            phase_review_parallel=MagicMock(),
            repair_dispatcher=MagicMock(),
            qe_resolution_checker=MagicMock(),
            cr_resolution_checker=MagicMock(),
            snapshot_workspace_files=None,
        )
        assert loop._collect_files_for_check(
            self._canonical_with_no_file_hints()
        ) == {}

    def test_old_path_still_collects_file_hints_when_no_discoverer(self):
        snapshot = MagicMock(
            return_value={"app/auth.py": "import jwt\n"}
        )
        canonical = CanonicalReport(
            milestone_name="M1",
            iter_frozen=1,
            findings=[
                CanonicalFinding(
                    id="cr:flagged_symbol:1",
                    source="code_reviewer",
                    priority="critical",
                    summary="bad import",
                    file_hint="app/auth.py",
                    status="initial",
                ),
            ],
        )
        loop = ReviewRepairV5Loop(
            phase_review_parallel=MagicMock(),
            repair_dispatcher=MagicMock(),
            qe_resolution_checker=MagicMock(),
            cr_resolution_checker=MagicMock(),
            snapshot_workspace_files=snapshot,
            discover_workspace_files=None,
        )
        files = loop._collect_files_for_check(canonical)
        assert "app/auth.py" in files
