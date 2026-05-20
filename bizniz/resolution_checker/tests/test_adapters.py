"""Tests for the iter-1 adapter functions."""
from __future__ import annotations

import pytest

from bizniz.canonical_findings.types import CanonicalFinding
from bizniz.code_reviewer.types import (
    AntiPatternViolation, CodeReviewReport, FlaggedSymbol,
    MissingErrorHandling, UngatedAuthCapability,
)
from bizniz.quality_engineer.types import CoverageReport, MissingScenario
from bizniz.resolution_checker.adapters import (
    cr_report_to_canonical_findings,
    qe_coverage_to_canonical_findings,
)


class TestQEAdapter:
    def test_missing_scenarios_become_findings(self):
        report = CoverageReport(
            milestone_name="M1",
            approved=False,
            missing_scenarios=[
                MissingScenario(capability_id="cap_a", scenario="first_login", priority="important"),
                MissingScenario(capability_id="cap_a", scenario="repeat_login", priority="critical"),
            ],
        )
        out = qe_coverage_to_canonical_findings(report)
        assert len(out) == 2
        # Priority normalization.
        priorities = sorted(f.priority for f in out)
        assert priorities == ["critical", "important"]
        # Capability id preserved.
        assert all(f.capability_id == "cap_a" for f in out)
        # Source.
        assert all(f.source == "quality_engineer" for f in out)

    def test_missing_capability_without_scenarios_surfaces(self):
        report = CoverageReport(
            milestone_name="M1",
            approved=False,
            coverage_by_capability={"cap_x": "missing"},
        )
        out = qe_coverage_to_canonical_findings(report)
        assert len(out) == 1
        assert out[0].capability_id == "cap_x"

    def test_capability_with_scenarios_doesnt_double_surface(self):
        report = CoverageReport(
            milestone_name="M1",
            approved=False,
            missing_scenarios=[
                MissingScenario(capability_id="cap_x", scenario="s1"),
            ],
            coverage_by_capability={"cap_x": "missing"},
        )
        out = qe_coverage_to_canonical_findings(report)
        # Only the scenario-level finding; the broader gap is implied.
        assert len(out) == 1

    def test_nice_to_have_priority_normalized(self):
        report = CoverageReport(
            milestone_name="M1", approved=False,
            missing_scenarios=[
                MissingScenario(capability_id="c", scenario="s", priority="nice-to-have"),
            ],
        )
        out = qe_coverage_to_canonical_findings(report)
        assert out[0].priority == "nice_to_have"


class TestCRAdapter:
    def test_critical_flagged_symbols_become_findings(self):
        report = CodeReviewReport(
            milestone_name="M1", approved=False,
            flagged_symbols=[
                FlaggedSymbol(
                    file="app/x.py", line=5, symbol="foo",
                    kind="import", reason="not in deps", severity="critical",
                ),
            ],
        )
        out = cr_report_to_canonical_findings(report)
        assert len(out) == 1
        assert out[0].priority == "critical"
        assert out[0].file_hint == "app/x.py"

    def test_warning_severity_filtered_out(self):
        """Only critical CR findings make it to canonical (warnings
        are advisory)."""
        report = CodeReviewReport(
            milestone_name="M1", approved=False,
            flagged_symbols=[
                FlaggedSymbol(
                    file="app/x.py", symbol="foo", kind="import",
                    reason="r", severity="warning",
                ),
            ],
        )
        out = cr_report_to_canonical_findings(report)
        assert out == []

    def test_anti_pattern_and_ungated_auth_surface(self):
        report = CodeReviewReport(
            milestone_name="M1", approved=False,
            anti_pattern_violations=[
                AntiPatternViolation(
                    file="app/y.py", anti_pattern="log_passwords",
                    evidence="logger.info(password)", severity="critical",
                ),
            ],
            ungated_auth=[
                UngatedAuthCapability(
                    file="app/z.py", capability_id="cap_a",
                    evidence="no get_current_user", severity="critical",
                ),
            ],
        )
        out = cr_report_to_canonical_findings(report)
        assert len(out) == 2
        summaries = [f.summary for f in out]
        assert any("anti-pattern" in s for s in summaries)
        assert any("ungated auth" in s for s in summaries)

    def test_capability_id_propagates_through_ungated_auth(self):
        report = CodeReviewReport(
            milestone_name="M1", approved=False,
            ungated_auth=[
                UngatedAuthCapability(
                    file="x.py", capability_id="cap_login",
                    evidence="e", severity="critical",
                ),
            ],
        )
        out = cr_report_to_canonical_findings(report)
        assert out[0].capability_id == "cap_login"


class TestFingerprintStability:
    def test_same_qe_input_same_id(self):
        ms = MissingScenario(capability_id="c", scenario="s")
        report1 = CoverageReport(milestone_name="M1", approved=False, missing_scenarios=[ms])
        report2 = CoverageReport(milestone_name="M1", approved=False, missing_scenarios=[ms])
        out1 = qe_coverage_to_canonical_findings(report1)
        out2 = qe_coverage_to_canonical_findings(report2)
        assert out1[0].id == out2[0].id

    def test_same_cr_input_same_id(self):
        fs = FlaggedSymbol(
            file="x.py", symbol="y", kind="import",
            reason="r", severity="critical",
        )
        r1 = CodeReviewReport(milestone_name="M1", approved=False, flagged_symbols=[fs])
        r2 = CodeReviewReport(milestone_name="M1", approved=False, flagged_symbols=[fs])
        out1 = cr_report_to_canonical_findings(r1)
        out2 = cr_report_to_canonical_findings(r2)
        assert out1[0].id == out2[0].id
