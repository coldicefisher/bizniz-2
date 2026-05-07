"""Tests for the v2.5 Orchestrator — drives Coder per-issue with
model escalation."""
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.coder.types import CoderResult, Issue
from bizniz.lib.dependency_graph import CyclicDependencyError
from bizniz.lib.model_progression import ModelProgression
from bizniz.lib.tool_loop_agent import ToolLoopAgentStalledError
from bizniz.orchestrator.orchestrator import Orchestrator
from bizniz.orchestrator.types import OrchestratorResult
from bizniz.quality_engineer.types import EnrichedSpec


# ── Fixtures ───────────────────────────────────────────────────────────


def _arch():
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="API",
                workspace_name="backend", port=8000, depends_on=[],
            ),
        ],
    )


def _spec():
    return EnrichedSpec(milestone_name="M1", capabilities=[])


def _issue(id_, deps=None, files=None):
    return Issue(
        id=id_, title=id_, description="",
        service="backend", language="python",
        target_files=files or [f"{id_}.py"],
        test_files=[f"tests/test_{id_}.py"],
        success_criteria=[],
        spec_refs=[],
        depends_on=deps or [],
    )


def _coder(side_effect):
    """Build a mock Coder.code_issue with the given side_effect."""
    coder = MagicMock()
    coder.code_issue.side_effect = side_effect
    return coder


def _result(issue_id, status="passed"):
    return CoderResult(issue_id=issue_id, status=status, summary="ok")


# ── Happy path ─────────────────────────────────────────────────────────


class TestHappyPath:
    def test_single_issue_passes_first_tier(self):
        coder = _coder([_result("I1")])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda model: coder,
            progression=ModelProgression(["lite", "top", "pro"]),
        )
        out = orch.run_service([_issue("I1")], _arch(), _spec())

        assert out.all_passed
        assert len(out.issues) == 1
        assert out.issues[0].disposition == "passed"
        assert out.issues[0].tiers_used == ["lite"]

    def test_multiple_issues_all_pass(self):
        coder = _coder([_result("I1"), _result("I2"), _result("I3")])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda model: coder,
            progression=ModelProgression(["lite", "top"]),
        )
        out = orch.run_service(
            [_issue("I1"), _issue("I2"), _issue("I3")],
            _arch(), _spec(),
        )
        assert out.all_passed
        assert out.passed_count == 3
        assert all(o.tiers_used == ["lite"] for o in out.issues)


# ── Topo ordering ──────────────────────────────────────────────────────


class TestTopoOrder:
    def test_respects_depends_on(self):
        # I3 depends on I2 depends on I1 — must fire in 1,2,3 order.
        coder = _coder([_result("I1"), _result("I2"), _result("I3")])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite"]),
        )
        out = orch.run_service(
            [_issue("I3", deps=["I2"]),
             _issue("I1"),
             _issue("I2", deps=["I1"])],
            _arch(), _spec(),
        )
        assert out.all_passed
        # CoderResult side_effect was queued I1, I2, I3 — so the issues
        # need to come back in the same order (topo).
        assert [o.issue_id for o in out.issues] == ["I1", "I2", "I3"]

    def test_cyclic_dependency_raises(self):
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: _coder([]),
            progression=ModelProgression(["lite"]),
        )
        with pytest.raises(CyclicDependencyError):
            orch.run_service(
                [_issue("A", deps=["B"]), _issue("B", deps=["A"])],
                _arch(), _spec(),
            )


# ── Escalation on stall ────────────────────────────────────────────────


class TestEscalation:
    def test_stall_escalates_then_passes(self):
        # Tier 0 stalls, tier 1 passes.
        coder = _coder([
            ToolLoopAgentStalledError("repetition"),
            _result("I1"),
        ])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite", "top", "pro"]),
        )
        out = orch.run_service([_issue("I1")], _arch(), _spec())

        assert out.all_passed
        assert out.issues[0].disposition == "escalated"
        assert out.issues[0].tiers_used == ["lite", "top"]

    def test_stall_at_every_tier_marks_stalled(self):
        coder = _coder([
            ToolLoopAgentStalledError("a"),
            ToolLoopAgentStalledError("b"),
            ToolLoopAgentStalledError("c"),
        ])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite", "top", "pro"]),
        )
        out = orch.run_service([_issue("I1")], _arch(), _spec())

        assert not out.all_passed
        assert out.issues[0].disposition == "stalled"
        assert out.issues[0].tiers_used == ["lite", "top", "pro"]
        assert "c" in out.issues[0].error

    def test_partial_status_escalates(self):
        # Tier 0 returns partial (forced-final at iteration cap),
        # tier 1 passes. Same escalation semantics as a stall.
        coder = _coder([
            CoderResult(issue_id="I1", status="partial", summary="iter cap"),
            _result("I1"),
        ])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite", "top"]),
        )
        out = orch.run_service([_issue("I1")], _arch(), _spec())

        assert out.all_passed
        assert out.issues[0].disposition == "escalated"
        assert out.issues[0].tiers_used == ["lite", "top"]

    def test_partial_at_top_tier_marks_partial(self):
        coder = _coder([
            CoderResult(issue_id="I1", status="partial", summary="lite"),
            CoderResult(issue_id="I1", status="partial", summary="top"),
        ])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite", "top"]),
        )
        out = orch.run_service([_issue("I1")], _arch(), _spec())

        assert not out.all_passed
        assert out.issues[0].disposition == "partial"
        assert out.issues[0].tiers_used == ["lite", "top"]
        assert out.issues[0].final_result is not None

    def test_failed_status_escalates(self):
        coder = _coder([
            CoderResult(issue_id="I1", status="failed", summary="bad"),
            _result("I1"),
        ])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite", "top"]),
        )
        out = orch.run_service([_issue("I1")], _arch(), _spec())
        assert out.all_passed
        assert out.issues[0].disposition == "escalated"

    def test_deferred_status_terminates_no_escalation(self):
        # Deferred = explicit punt; do not escalate.
        coder = _coder([
            CoderResult(issue_id="I1", status="deferred",
                        summary="needs upstream"),
        ])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite", "top"]),
        )
        out = orch.run_service([_issue("I1")], _arch(), _spec())
        assert out.issues[0].disposition == "deferred"
        assert out.issues[0].tiers_used == ["lite"]

    def test_progression_resets_per_issue(self):
        # I1 escalates to top, I2 should start fresh at lite.
        coder = _coder([
            ToolLoopAgentStalledError("a"),
            _result("I1"),
            _result("I2"),
        ])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite", "top", "pro"]),
        )
        out = orch.run_service([_issue("I1"), _issue("I2")], _arch(), _spec())

        assert out.all_passed
        assert out.issues[0].tiers_used == ["lite", "top"]
        assert out.issues[1].tiers_used == ["lite"]


# ── Skipped on dep failure ─────────────────────────────────────────────


class TestSkipOnDepFailure:
    def test_dependent_skipped_when_dependency_stalls(self):
        coder = _coder([
            ToolLoopAgentStalledError("a"),  # I1 tier 0
            ToolLoopAgentStalledError("b"),  # I1 tier 1
        ])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite", "top"]),
        )
        out = orch.run_service(
            [_issue("I1"), _issue("I2", deps=["I1"])],
            _arch(), _spec(),
        )

        assert out.issues[0].disposition == "stalled"
        assert out.issues[1].disposition == "skipped"
        # I2 was not dispatched to the Coder
        assert coder.code_issue.call_count == 2  # only I1's two tiers


# ── Unexpected exception handling ──────────────────────────────────────


class TestUnexpectedException:
    def test_unexpected_exception_marked_errored(self):
        coder = _coder([RuntimeError("boom")])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite"]),
        )
        out = orch.run_service([_issue("I1")], _arch(), _spec())

        assert out.issues[0].disposition == "errored"
        assert "RuntimeError" in out.issues[0].error
        assert "boom" in out.issues[0].error


# ── on_status callback ─────────────────────────────────────────────────


class TestStatusCallback:
    def test_status_callback_fires(self):
        statuses: list = []
        coder = _coder([_result("I1")])
        orch = Orchestrator(
            service="backend",
            coder_factory=lambda m: coder,
            progression=ModelProgression(["lite"]),
            on_status=statuses.append,
        )
        orch.run_service([_issue("I1")], _arch(), _spec())
        assert any("I1" in s for s in statuses)
        assert any("starting" in s for s in statuses)
