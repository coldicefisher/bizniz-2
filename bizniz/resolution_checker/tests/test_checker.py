"""Tests for ResolutionChecker — mocks the LLM call."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bizniz.canonical_findings.types import (
    CanonicalFinding, CanonicalReport,
)
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.resolution_checker.checker import (
    ResolutionChecker, check_both_sources_parallel,
)


def _canonical(*, findings) -> CanonicalReport:
    return CanonicalReport(milestone_name="M1", findings=findings)


def _qe_finding(id="f1", status="initial") -> CanonicalFinding:
    return CanonicalFinding(
        id=id, source="quality_engineer", priority="critical",
        summary="s", status=status,
    )


def _cr_finding(id="f2", status="initial") -> CanonicalFinding:
    return CanonicalFinding(
        id=id, source="code_reviewer", priority="critical",
        summary="s", status=status,
    )


class TestSkipsResolvedAndWontFix:
    def test_resolved_findings_not_sent_to_llm(self):
        canonical = _canonical(findings=[
            _qe_finding("f1", status="resolved"),
            _qe_finding("f2", status="wont_fix"),
        ])
        checker = ResolutionChecker(client=MagicMock(spec=BaseAIClient))
        with patch(
            "bizniz.resolution_checker.checker.call_with_retry",
        ) as mock_call:
            report = checker.check(
                canonical=canonical, iter_idx=2, current_files={},
            )
        # No LLM call when there's nothing to check.
        mock_call.assert_not_called()
        assert report.resolutions == []

    def test_only_active_findings_in_prompt(self):
        canonical = _canonical(findings=[
            _qe_finding("active1", status="initial"),
            _qe_finding("done1", status="resolved"),
            _qe_finding("active2", status="still_present"),
        ])
        checker = ResolutionChecker(client=MagicMock(spec=BaseAIClient))
        with patch(
            "bizniz.resolution_checker.checker.call_with_retry",
            return_value={"resolutions": []},
        ) as mock_call:
            checker.check(
                canonical=canonical, iter_idx=2, current_files={},
            )
        prompt = mock_call.call_args.kwargs["messages"][1].content
        assert "active1" in prompt and "active2" in prompt
        assert "done1" not in prompt


class TestSourceFilter:
    def test_qe_only_filters_cr_findings(self):
        canonical = _canonical(findings=[
            _qe_finding("qe1"),
            _cr_finding("cr1"),
            _qe_finding("qe2"),
        ])
        checker = ResolutionChecker(client=MagicMock(spec=BaseAIClient))
        with patch(
            "bizniz.resolution_checker.checker.call_with_retry",
            return_value={"resolutions": []},
        ) as mock_call:
            checker.check(
                canonical=canonical, iter_idx=2, current_files={},
                source_filter="quality_engineer",
            )
        prompt = mock_call.call_args.kwargs["messages"][1].content
        assert "qe1" in prompt and "qe2" in prompt
        assert "cr1" not in prompt


class TestUnknownIdsDropped:
    def test_resolution_for_unknown_id_dropped_from_report(self):
        canonical = _canonical(findings=[_qe_finding("real1")])
        checker = ResolutionChecker(client=MagicMock(spec=BaseAIClient))
        with patch(
            "bizniz.resolution_checker.checker.call_with_retry",
            return_value={
                "resolutions": [
                    {"finding_id": "real1", "status": "resolved", "evidence": "ok"},
                    {"finding_id": "FAKE", "status": "still_present", "evidence": "x"},
                ],
            },
        ):
            report = checker.check(
                canonical=canonical, iter_idx=2, current_files={},
            )
        ids = [r.finding_id for r in report.resolutions]
        assert ids == ["real1"]


class TestCurrentFilesIncluded:
    def test_file_contents_appear_in_prompt(self):
        canonical = _canonical(findings=[_qe_finding("f1")])
        checker = ResolutionChecker(client=MagicMock(spec=BaseAIClient))
        with patch(
            "bizniz.resolution_checker.checker.call_with_retry",
            return_value={"resolutions": []},
        ) as mock_call:
            checker.check(
                canonical=canonical, iter_idx=2,
                current_files={"app/me.py": "def me(): return 1\n"},
            )
        prompt = mock_call.call_args.kwargs["messages"][1].content
        assert "app/me.py" in prompt
        assert "def me()" in prompt


class TestParallelFanOut:
    def test_check_both_sources_parallel_merges_results(self):
        canonical = _canonical(findings=[
            _qe_finding("qe1"),
            _cr_finding("cr1"),
        ])
        qe_check = MagicMock(spec=ResolutionChecker)
        cr_check = MagicMock(spec=ResolutionChecker)
        from bizniz.canonical_findings.types import (
            FindingResolution, ResolutionReport,
        )
        qe_check.check.return_value = ResolutionReport(
            milestone_name="M1", iter_idx=2,
            resolutions=[
                FindingResolution(finding_id="qe1", status="resolved", evidence="ok"),
            ],
        )
        cr_check.check.return_value = ResolutionReport(
            milestone_name="M1", iter_idx=2,
            resolutions=[
                FindingResolution(finding_id="cr1", status="still_present", evidence="ok"),
            ],
        )
        merged = check_both_sources_parallel(
            qe_checker=qe_check, cr_checker=cr_check,
            canonical=canonical, iter_idx=2, current_files={},
        )
        ids = {r.finding_id for r in merged.resolutions}
        assert ids == {"qe1", "cr1"}
        qe_check.check.assert_called_once()
        cr_check.check.assert_called_once()
