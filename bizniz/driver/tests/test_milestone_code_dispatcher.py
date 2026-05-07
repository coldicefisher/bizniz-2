"""Tests for MilestoneCodeDispatcher — wraps ServicePlanner +
Orchestrator + Coder into an EngineerResult-shaped output."""
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.coder.types import CoderResult, Issue as CoderIssue
from bizniz.driver.milestone_code_dispatcher import MilestoneCodeDispatcher
from bizniz.engineer.types import EngineerResult
from bizniz.lib.model_progression import ModelProgression
from bizniz.lib.tool_loop_agent import ToolLoopAgentStalledError
from bizniz.quality_engineer.types import EnrichedSpec


# ── Fixtures ───────────────────────────────────────────────────────────


def _backend():
    return ServiceDefinition(
        name="backend", service_type="backend", framework="fastapi",
        language="python", description="API",
        workspace_name="backend", port=8000, depends_on=["db"],
    )


def _db():
    return ServiceDefinition(
        name="db", service_type="database", framework="postgres",
        language="sql", description="db",
        workspace_name="db", port=5432, depends_on=[],
    )


def _arch(services=None):
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=services or [_backend(), _db()],
    )


def _spec():
    return EnrichedSpec(milestone_name="M1", capabilities=[])


def _coder_issue(id_, service="backend", deps=None):
    return CoderIssue(
        id=id_, title=id_, description="",
        service=service, language="python",
        target_files=[f"{id_}.py"],
        test_files=[f"tests/test_{id_}.py"],
        success_criteria=[], spec_refs=[],
        depends_on=deps or [],
    )


def _planner_factory_returning(per_service_issues):
    """per_service_issues: dict[service_name → List[CoderIssue]]"""
    def factory(service):
        planner = MagicMock()
        planner.plan_service.return_value = per_service_issues.get(service.name, [])
        return planner
    return factory


def _coder_factory_with(coder_results):
    """coder_results: list of CoderResult OR Exception, drained in order."""
    coder = MagicMock()
    coder.code_issue.side_effect = list(coder_results)

    def factory(model, service):
        return coder
    return factory


def _progression_factory(models=("lite", "top", "pro")):
    def factory(service):
        return ModelProgression(list(models))
    return factory


# ── Happy path ─────────────────────────────────────────────────────────


class TestHappyPath:
    def test_single_service_all_pass(self):
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "backend": [_coder_issue("BE-001"), _coder_issue("BE-002")],
            }),
            coder_factory=_coder_factory_with([
                CoderResult(issue_id="BE-001", status="passed", summary=""),
                CoderResult(issue_id="BE-002", status="passed", summary=""),
            ]),
            progression_factory=_progression_factory(),
        )

        result = dispatcher.run(
            architecture=_arch(services=[_backend()]),
            enriched_spec=_spec(),
        )
        assert isinstance(result, EngineerResult)
        assert result.final_test_status == "passed"
        assert result.completed_issue_ids == ["BE-001", "BE-002"]
        assert result.deferred_issue_ids == []
        assert len(result.plan.issues) == 2
        assert all(i.status == "done" for i in result.plan.issues)

    def test_two_services_topo_ordered(self):
        # db plans first (no deps), backend plans second (depends_on db).
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "db": [_coder_issue("DB-001", service="db")],
                "backend": [_coder_issue("BE-001")],
            }),
            coder_factory=_coder_factory_with([
                CoderResult(issue_id="DB-001", status="passed"),
                CoderResult(issue_id="BE-001", status="passed"),
            ]),
            progression_factory=_progression_factory(),
        )
        result = dispatcher.run(architecture=_arch(), enriched_spec=_spec())

        # db's issue must come before backend's in the plan
        ids = [i.id for i in result.plan.issues]
        assert ids.index("DB-001") < ids.index("BE-001")


# ── Mixed pass/fail ────────────────────────────────────────────────────


class TestMixed:
    def test_partial_failure(self):
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "backend": [_coder_issue("BE-001"), _coder_issue("BE-002")],
            }),
            coder_factory=_coder_factory_with([
                CoderResult(issue_id="BE-001", status="passed"),
                ToolLoopAgentStalledError("a"),
                ToolLoopAgentStalledError("b"),
                ToolLoopAgentStalledError("c"),
            ]),
            progression_factory=_progression_factory(["lite", "top", "pro"]),
        )
        result = dispatcher.run(
            architecture=_arch(services=[_backend()]),
            enriched_spec=_spec(),
        )
        assert result.final_test_status == "partial"
        assert "BE-001" in result.completed_issue_ids
        assert "BE-002" in result.deferred_issue_ids
        statuses = {i.id: i.status for i in result.plan.issues}
        assert statuses["BE-001"] == "done"
        assert statuses["BE-002"] == "blocked"
        assert any("BE-002" in n for n in result.notes)

    def test_all_failed(self):
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "backend": [_coder_issue("BE-001")],
            }),
            coder_factory=_coder_factory_with([
                ToolLoopAgentStalledError("a"),
            ]),
            progression_factory=_progression_factory(["lite"]),
        )
        result = dispatcher.run(
            architecture=_arch(services=[_backend()]),
            enriched_spec=_spec(),
        )
        assert result.final_test_status == "failed"
        assert result.completed_issue_ids == []


# ── Empty plan ─────────────────────────────────────────────────────────


class TestEmpty:
    def test_no_services_returns_not_run(self):
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({}),
            coder_factory=_coder_factory_with([]),
            progression_factory=_progression_factory(),
        )
        result = dispatcher.run(
            architecture=SystemArchitecture(
                project_name="P", project_slug="p",
                description="d", services=[],
            ),
            enriched_spec=_spec(),
        )
        assert result.final_test_status == "not_run"
        assert result.plan.issues == []


# ── Skeleton + auth threading ──────────────────────────────────────────


class TestThreading:
    def test_skeleton_for_service_propagates(self):
        recorded = {}

        def planner_factory(service):
            planner = MagicMock()

            def capture(**kwargs):
                recorded["skeleton"] = kwargs.get("skeleton_md")
                recorded["auth"] = kwargs.get("auth_contract")
                return [_coder_issue("BE-001")]

            planner.plan_service.side_effect = capture
            return planner

        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=planner_factory,
            coder_factory=_coder_factory_with([
                CoderResult(issue_id="BE-001", status="passed"),
            ]),
            progression_factory=_progression_factory(),
        )
        dispatcher.run(
            architecture=_arch(services=[_backend()]),
            enriched_spec=_spec(),
            auth_contract="JWT-rs256",
            skeleton_md_for_service=lambda s: f"## skel for {s}",
        )
        assert recorded["skeleton"] == "## skel for backend"
        assert recorded["auth"] == "JWT-rs256"
