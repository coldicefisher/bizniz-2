"""Tests for driver.milestone_loop — sub-phase resume + repair escalation.

End-to-end with mocked agents. Covers:
  - Happy path: enrich → implement → review approves → integration passes
  - Repair loop: review fails → repair → review approves
  - Repair budget exhausted → halt
  - Resume: state pre-populated → loop skips done phases
  - Integration api fail halts; web fail halts
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.code_reviewer.types import (
    CodeReviewReport, FlaggedSymbol,
)
from bizniz.driver.gates import GatePolicy, GateViolation
from bizniz.driver.integration_phase import IntegrationPhaseResult
from bizniz.driver.milestone_loop import (
    MilestoneLoop, MilestoneOutcome, _merge_to_repair_report,
)
from bizniz.driver.state import MilestoneState, SubPhase
from bizniz.engineer.types import EngineerPlan, EngineerResult, Issue
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.types import (
    CapabilitySpec, CoverageReport, EnrichedSpec, MissingScenario,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _arch():
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=[ServiceDefinition(
            name="backend", service_type="backend", framework="fastapi",
            language="python", description="API",
            workspace_name="backend", port=8000,
        )],
    )


def _milestone():
    return Milestone(
        sequence_index=1, name="M1", problem_slice="x",
    )


def _spec(approved=True):
    return EnrichedSpec(
        milestone_name="M1",
        capabilities=[CapabilitySpec(
            id="c0", name="N", description="d",
            inputs=[], outputs=[], validation_rules=[], error_cases=[],
            edge_cases=[], auth_required=True, allowed_roles=[],
            test_scenarios=[],
        )],
    )


def _engineer_result():
    return EngineerResult(
        plan=EngineerPlan(approach="ok", issues=[Issue(
            id="I1", title="t", description="d",
            spec_refs=["c0"],
        )]),
        final_test_status="passed",
    )


def _coverage(approved=True):
    return CoverageReport(
        milestone_name="M1", approved=approved,
        coverage_by_capability={"c0": "covered" if approved else "missing"},
    )


def _code_review(approved=True, critical=False):
    flagged = []
    if critical:
        flagged.append(FlaggedSymbol(
            file="x.py", line=1, symbol="ghost", kind="import",
            reason="fake", severity="critical",
        ))
    return CodeReviewReport(
        milestone_name="M1",
        approved=approved and not critical,
        flagged_symbols=flagged,
    )


def _integration_result(passed=True, phase="api"):
    return IntegrationPhaseResult(
        phase=phase, passed=passed,
        backend_contracts={"backend": {}} if phase == "api" else {},
    )


def _build_loop(
    *,
    engineer=None, qe=None, cr=None, integration=None,
    workspace=None, gates=None, factory=None,
):
    eng = engineer or MagicMock()
    qe_m = qe or MagicMock()
    cr_m = cr or MagicMock()
    ip = integration or MagicMock()
    ws = workspace or MagicMock()
    g = gates or GatePolicy(mode="strict")

    return MilestoneLoop(
        engineer=eng,
        quality_engineer=qe_m,
        code_reviewer=cr_m,
        integration_phase=ip,
        gates=g,
        workspace_for_service=lambda name: ws,
        primary_workspace=ws,
        compose_path="/p/c.yml",
        project_root=Path("/p"),
        repair_budget=3,
        repair_engineer_factory=factory,
    )


# ── Happy path ──────────────────────────────────────────────────────────


class TestHappyPath:
    def test_enrich_implement_review_pass_integration(self, tmp_path):
        eng = MagicMock()
        eng.implement.return_value = _engineer_result()
        qe = MagicMock()
        qe.enrich.return_value = _spec()
        qe.review.return_value = _coverage(approved=True)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=True)
        ip = MagicMock()
        ip.run_api.return_value = _integration_result(passed=True, phase="api")
        ip.run_web.return_value = _integration_result(passed=True, phase="web")

        loop = _build_loop(engineer=eng, qe=qe, cr=cr, integration=ip)
        state = MilestoneState(tmp_path / "m1", 1)

        outcome = loop.run(
            milestone=_milestone(), architecture=_arch(),
            prior_specs=[], auth_contract=None, state=state,
        )
        assert outcome.final_subphase == SubPhase.DONE
        assert outcome.repair_iterations == 0
        # No repair iterations means engineer.repair never called.
        eng.repair.assert_not_called()
        # Integration phases ran.
        ip.run_api.assert_called_once()
        ip.run_web.assert_called_once()
        # State should reflect DONE.
        s2 = MilestoneState(tmp_path / "m1", 1)
        assert s2.is_done()


# ── Repair loop ─────────────────────────────────────────────────────────


class TestRepairLoop:
    def test_repair_succeeds_on_first_iteration(self, tmp_path):
        eng = MagicMock()
        eng.implement.return_value = _engineer_result()
        eng.repair.return_value = _engineer_result()
        qe = MagicMock()
        qe.enrich.return_value = _spec()
        # First review fails, repair review succeeds.
        qe.review.side_effect = [_coverage(approved=False), _coverage(approved=True)]
        cr = MagicMock()
        cr.review.side_effect = [_code_review(approved=False, critical=True), _code_review(approved=True)]
        ip = MagicMock()
        ip.run_api.return_value = _integration_result(phase="api")
        ip.run_web.return_value = _integration_result(phase="web")

        loop = _build_loop(engineer=eng, qe=qe, cr=cr, integration=ip)
        state = MilestoneState(tmp_path / "m1", 1)

        outcome = loop.run(
            milestone=_milestone(), architecture=_arch(),
            prior_specs=[], auth_contract=None, state=state,
        )
        assert outcome.final_subphase == SubPhase.DONE
        assert outcome.repair_iterations == 1
        eng.repair.assert_called_once()

    def test_repair_budget_exhausted_halts(self, tmp_path):
        eng = MagicMock()
        eng.implement.return_value = _engineer_result()
        eng.repair.return_value = _engineer_result()
        qe = MagicMock()
        qe.enrich.return_value = _spec()
        # Always fails — should consume entire budget then halt.
        qe.review.return_value = _coverage(approved=False)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=False, critical=True)
        ip = MagicMock()

        loop = _build_loop(engineer=eng, qe=qe, cr=cr, integration=ip)
        state = MilestoneState(tmp_path / "m1", 1)

        with pytest.raises(GateViolation) as exc:
            loop.run(
                milestone=_milestone(), architecture=_arch(),
                prior_specs=[], auth_contract=None, state=state,
            )
        assert exc.value.gate_name == "milestone_unapproved"
        # Budget = 3, so engineer.repair called 3 times.
        assert eng.repair.call_count == 3
        # Integration should NOT have run.
        ip.run_api.assert_not_called()

    def test_factory_used_when_provided(self, tmp_path):
        eng = MagicMock()
        eng.implement.return_value = _engineer_result()
        # Repair tier engines: distinct mock instances, returning distinct results.
        tier_engineers = [MagicMock(), MagicMock(), MagicMock()]
        for te in tier_engineers:
            te.repair.return_value = _engineer_result()
        factory = MagicMock(side_effect=lambda i: tier_engineers[i])
        qe = MagicMock()
        qe.enrich.return_value = _spec()
        # First two fails, third succeeds.
        qe.review.side_effect = [
            _coverage(approved=False), _coverage(approved=False),
            _coverage(approved=False), _coverage(approved=True),
        ]
        cr = MagicMock()
        cr.review.side_effect = [
            _code_review(approved=False, critical=True),
            _code_review(approved=False, critical=True),
            _code_review(approved=False, critical=True),
            _code_review(approved=True),
        ]
        ip = MagicMock()
        ip.run_api.return_value = _integration_result(phase="api")
        ip.run_web.return_value = _integration_result(phase="web")

        loop = _build_loop(
            engineer=eng, qe=qe, cr=cr, integration=ip, factory=factory,
        )
        state = MilestoneState(tmp_path / "m1", 1)
        outcome = loop.run(
            milestone=_milestone(), architecture=_arch(),
            prior_specs=[], auth_contract=None, state=state,
        )
        # Factory called with 0, 1, 2 (each repair iteration).
        assert factory.call_count == 3
        assert tier_engineers[0].repair.called
        assert tier_engineers[1].repair.called
        assert tier_engineers[2].repair.called
        # Default engineer.repair NOT called (factory took precedence).
        eng.repair.assert_not_called()
        assert outcome.final_subphase == SubPhase.DONE


# ── Resume ──────────────────────────────────────────────────────────────


class TestResume:
    def test_skips_completed_phases(self, tmp_path):
        # Pre-populate state through implement.
        state = MilestoneState(tmp_path / "m1", 1)
        state.mark_phase(SubPhase.ENRICH, _spec())
        state.mark_phase(SubPhase.IMPLEMENT, _engineer_result())

        eng = MagicMock()
        qe = MagicMock()
        qe.review.return_value = _coverage(approved=True)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=True)
        ip = MagicMock()
        ip.run_api.return_value = _integration_result(phase="api")
        ip.run_web.return_value = _integration_result(phase="web")

        loop = _build_loop(engineer=eng, qe=qe, cr=cr, integration=ip)
        outcome = loop.run(
            milestone=_milestone(), architecture=_arch(),
            prior_specs=[], auth_contract=None, state=state,
        )
        # Skipped phases should not have called the agents.
        qe.enrich.assert_not_called()
        eng.implement.assert_not_called()
        # Review still ran.
        qe.review.assert_called()
        assert outcome.final_subphase == SubPhase.DONE

    def test_skips_done_milestone_in_pipeline_pattern(self, tmp_path):
        # Milestone marked DONE → loop should not redo anything.
        # (Pipeline checks ms_state.is_done() before calling, but
        # ensure loop.run is also defensive.)
        state = MilestoneState(tmp_path / "m1", 1)
        state.mark_phase(SubPhase.ENRICH, _spec())
        state.mark_phase(SubPhase.IMPLEMENT, _engineer_result())
        state.mark_phase(SubPhase.REVIEW_INITIAL, {
            "coverage": _coverage(approved=True).model_dump(),
            "code_review": _code_review(approved=True).model_dump(),
        })
        state.mark_phase(SubPhase.REVIEW_FINAL, {
            "coverage": _coverage(approved=True).model_dump(),
            "code_review": _code_review(approved=True).model_dump(),
        })
        state.mark_phase(SubPhase.INTEGRATION_API, _integration_result(phase="api").model_dump())
        state.mark_phase(SubPhase.INTEGRATION_WEB, _integration_result(phase="web").model_dump())

        eng = MagicMock()
        qe = MagicMock()
        cr = MagicMock()
        ip = MagicMock()
        loop = _build_loop(engineer=eng, qe=qe, cr=cr, integration=ip)

        outcome = loop.run(
            milestone=_milestone(), architecture=_arch(),
            prior_specs=[], auth_contract=None, state=state,
        )
        # Nothing should have run.
        eng.implement.assert_not_called()
        ip.run_api.assert_not_called()
        ip.run_web.assert_not_called()
        assert outcome.final_subphase == SubPhase.DONE


# ── Integration gates ──────────────────────────────────────────────────


class TestIntegrationGates:
    def test_api_failure_halts(self, tmp_path):
        eng = MagicMock()
        eng.implement.return_value = _engineer_result()
        qe = MagicMock()
        qe.enrich.return_value = _spec()
        qe.review.return_value = _coverage(approved=True)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=True)
        ip = MagicMock()
        ip.run_api.return_value = _integration_result(passed=False, phase="api")

        loop = _build_loop(engineer=eng, qe=qe, cr=cr, integration=ip)
        state = MilestoneState(tmp_path / "m1", 1)
        with pytest.raises(GateViolation) as exc:
            loop.run(
                milestone=_milestone(), architecture=_arch(),
                prior_specs=[], auth_contract=None, state=state,
            )
        assert exc.value.gate_name == "integration_api_failed"
        ip.run_web.assert_not_called()  # Web should not run after API fail.

    def test_web_failure_halts(self, tmp_path):
        eng = MagicMock()
        eng.implement.return_value = _engineer_result()
        qe = MagicMock()
        qe.enrich.return_value = _spec()
        qe.review.return_value = _coverage(approved=True)
        cr = MagicMock()
        cr.review.return_value = _code_review(approved=True)
        ip = MagicMock()
        ip.run_api.return_value = _integration_result(phase="api")
        ip.run_web.return_value = _integration_result(passed=False, phase="web")

        loop = _build_loop(engineer=eng, qe=qe, cr=cr, integration=ip)
        state = MilestoneState(tmp_path / "m1", 1)
        with pytest.raises(GateViolation) as exc:
            loop.run(
                milestone=_milestone(), architecture=_arch(),
                prior_specs=[], auth_contract=None, state=state,
            )
        assert exc.value.gate_name == "integration_web_failed"


# ── Merge helper ────────────────────────────────────────────────────────


class TestMergeRepairReport:
    def test_includes_critical_findings(self):
        cr = _code_review(approved=False, critical=True)
        merged = _merge_to_repair_report("M1", _coverage(approved=True), cr)
        assert merged.approved is False
        assert len(merged.flagged_symbols) == 1

    def test_promotes_critical_missing_scenarios(self):
        coverage = CoverageReport(
            milestone_name="M1", approved=False,
            coverage_by_capability={"c0": "covered"},
            missing_scenarios=[MissingScenario(
                capability_id="c0", scenario="auth bypass",
                priority="critical",
            )],
        )
        merged = _merge_to_repair_report("M1", coverage, _code_review(approved=True))
        # The critical missing_scenario should land in
        # missing_error_handling with severity critical.
        crit = [m for m in merged.missing_error_handling if m.severity == "critical"]
        assert any("auth bypass" in m.error_case for m in crit)

    def test_missing_capability_promoted_to_critical(self):
        coverage = CoverageReport(
            milestone_name="M1", approved=False,
            coverage_by_capability={"c0": "missing"},
        )
        merged = _merge_to_repair_report("M1", coverage, _code_review(approved=True))
        crit = [m for m in merged.missing_error_handling if m.severity == "critical"]
        assert any(m.capability_id == "c0" for m in crit)
