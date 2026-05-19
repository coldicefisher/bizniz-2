"""Tests for the per-source adapters."""
from __future__ import annotations

import pytest

from bizniz.code_reviewer.types import (
    AntiPatternViolation, CodeReviewReport, FlaggedSymbol,
    MissingErrorHandling, UngatedAuthCapability,
)
from bizniz.quality_engineer.types import CoverageReport, MissingScenario
from bizniz.review_unit.adapters.code_reviewer import cr_report_to_findings
from bizniz.review_unit.adapters.quality_engineer import qe_coverage_to_findings


class TestQEAdapter:
    def test_empty_report_yields_no_findings(self):
        r = CoverageReport(milestone_name="M1", approved=True)
        assert qe_coverage_to_findings(r) == []

    def test_missing_capability_becomes_high_severity_finding(self):
        r = CoverageReport(
            milestone_name="M1", approved=False,
            coverage_by_capability={"cap.foo": "missing"},
        )
        out = qe_coverage_to_findings(r)
        assert len(out) == 1
        assert out[0].source == "quality_engineer"
        assert out[0].severity == "high"
        assert out[0].fingerprint == "cap.cap.foo.missing"
        assert "cap.foo" in out[0].message

    def test_partial_capability_becomes_medium_severity_finding(self):
        r = CoverageReport(
            milestone_name="M1", approved=False,
            coverage_by_capability={"cap.bar": "partial"},
        )
        out = qe_coverage_to_findings(r)
        assert len(out) == 1
        assert out[0].severity == "medium"
        assert out[0].fingerprint == "cap.cap.bar.partial"

    def test_covered_capability_emits_no_finding(self):
        r = CoverageReport(
            milestone_name="M1", approved=True,
            coverage_by_capability={"cap.ok": "covered"},
        )
        assert qe_coverage_to_findings(r) == []

    def test_missing_scenario_priority_maps_to_severity(self):
        r = CoverageReport(
            milestone_name="M1", approved=False,
            missing_scenarios=[
                MissingScenario(capability_id="c1", scenario="crit one", priority="critical"),
                MissingScenario(capability_id="c2", scenario="imp one", priority="important"),
                MissingScenario(capability_id="c3", scenario="nice one", priority="nice-to-have"),
            ],
        )
        out = qe_coverage_to_findings(r)
        sevs = sorted(f.severity for f in out)
        assert sevs == ["critical", "high", "medium"]

    def test_recommendations_become_low_severity(self):
        r = CoverageReport(
            milestone_name="M1", approved=True,
            recommendations=["consider adding tests for X"],
        )
        out = qe_coverage_to_findings(r)
        assert len(out) == 1
        assert out[0].severity == "low"
        assert out[0].fingerprint == "qe.recommendation.0"

    def test_fingerprint_stable_across_calls(self):
        ms = MissingScenario(capability_id="c1", scenario="exact scenario string", priority="critical")
        r1 = CoverageReport(milestone_name="M1", approved=False, missing_scenarios=[ms])
        r2 = CoverageReport(milestone_name="M1", approved=False, missing_scenarios=[ms])
        out1 = qe_coverage_to_findings(r1)
        out2 = qe_coverage_to_findings(r2)
        assert out1[0].fingerprint == out2[0].fingerprint


class TestCRAdapter:
    def test_empty_report_yields_no_findings(self):
        r = CodeReviewReport(milestone_name="M1", approved=True)
        assert cr_report_to_findings(r) == []

    def test_flagged_symbol_critical_maps_critical(self):
        r = CodeReviewReport(
            milestone_name="M1", approved=False,
            flagged_symbols=[FlaggedSymbol(
                file="app/x.py", line=5, symbol="fake_fn",
                kind="function_call", reason="not defined anywhere",
                severity="critical",
            )],
        )
        out = cr_report_to_findings(r)
        assert len(out) == 1
        assert out[0].severity == "critical"
        assert out[0].source == "code_reviewer"
        assert out[0].file_path == "app/x.py"
        assert out[0].line == 5
        assert "fake_fn" in out[0].fingerprint

    def test_flagged_symbol_warning_maps_medium(self):
        r = CodeReviewReport(
            milestone_name="M1", approved=False,
            flagged_symbols=[FlaggedSymbol(
                file="app/x.py", symbol="maybe_fake",
                kind="import", reason="suspicious",
                severity="warning",
            )],
        )
        out = cr_report_to_findings(r)
        assert out[0].severity == "medium"

    def test_anti_pattern_violation(self):
        r = CodeReviewReport(
            milestone_name="M1", approved=False,
            anti_pattern_violations=[AntiPatternViolation(
                file="app/x.py", line=12,
                anti_pattern="never log raw passwords",
                evidence="log.info(password)",
                severity="critical",
            )],
        )
        out = cr_report_to_findings(r)
        assert len(out) == 1
        assert out[0].severity == "critical"
        assert "anti-pattern" in out[0].message.lower() or "Anti-pattern" in out[0].message
        assert out[0].fingerprint.startswith("cr.anti.")

    def test_ungated_auth(self):
        r = CodeReviewReport(
            milestone_name="M1", approved=False,
            ungated_auth=[UngatedAuthCapability(
                file="app/routes/me.py", capability_id="cap.me",
                evidence="@router.get('/me') without Depends(get_current_user)",
                severity="critical",
            )],
        )
        out = cr_report_to_findings(r)
        assert out[0].fingerprint == "cr.auth.cap.me"
        assert out[0].severity == "critical"

    def test_missing_error_handling_warning_maps_medium(self):
        r = CodeReviewReport(
            milestone_name="M1", approved=True,
            missing_error_handling=[MissingErrorHandling(
                capability_id="cap.foo",
                error_case="500 when DB is down",
                severity="warning",
            )],
        )
        out = cr_report_to_findings(r)
        assert out[0].severity == "medium"

    def test_all_categories_aggregate(self):
        r = CodeReviewReport(
            milestone_name="M1", approved=False,
            flagged_symbols=[FlaggedSymbol(
                file="a.py", symbol="x", kind="import",
                reason="r", severity="critical",
            )],
            anti_pattern_violations=[AntiPatternViolation(
                file="b.py", anti_pattern="ap", evidence="e",
                severity="critical",
            )],
            ungated_auth=[UngatedAuthCapability(
                file="c.py", capability_id="cap", evidence="e",
                severity="critical",
            )],
            missing_error_handling=[MissingErrorHandling(
                capability_id="cap", error_case="ec",
                severity="critical",
            )],
        )
        out = cr_report_to_findings(r)
        assert len(out) == 4
        sources = {f.source for f in out}
        assert sources == {"code_reviewer"}
