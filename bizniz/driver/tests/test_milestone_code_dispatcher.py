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
        # worker plans first (no deps), backend plans second
        # (depends_on worker). Both are code-bearing (python) so the
        # language filter doesn't skip either.
        worker = ServiceDefinition(
            name="worker", service_type="worker", framework="celery",
            language="python", description="bg worker",
            workspace_name="worker", port=None, depends_on=[],
        )
        backend_dep_worker = ServiceDefinition(
            name="backend", service_type="backend", framework="fastapi",
            language="python", description="API",
            workspace_name="backend", port=8000, depends_on=["worker"],
        )
        arch = SystemArchitecture(
            project_name="P", project_slug="p", description="",
            services=[backend_dep_worker, worker],  # declared out of order on purpose
        )

        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "worker": [_coder_issue("WK-001", service="worker")],
                "backend": [_coder_issue("BE-001")],
            }),
            coder_factory=_coder_factory_with([
                CoderResult(issue_id="WK-001", status="passed"),
                CoderResult(issue_id="BE-001", status="passed"),
            ]),
            progression_factory=_progression_factory(),
        )
        result = dispatcher.run(architecture=arch, enriched_spec=_spec())

        # worker's issue must come before backend's in the plan
        ids = [i.id for i in result.plan.issues]
        assert ids.index("WK-001") < ids.index("BE-001")


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


# ── Code-bearing filter ────────────────────────────────────────────────


class TestCodeBearingFilter:
    def test_db_service_is_skipped(self):
        # Pure-infrastructure services (sql, conf, yaml) must NOT
        # be planned — the pytest sidecar can't green-test them.
        planner_called: list = []

        def planner_factory(service):
            planner_called.append(service.name)
            planner = MagicMock()
            planner.plan_service.return_value = []
            return planner

        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=planner_factory,
            coder_factory=_coder_factory_with([]),
            progression_factory=_progression_factory(),
        )
        # Architecture has db (sql), cache (redis-conf), backend (python)
        cache = ServiceDefinition(
            name="cache", service_type="cache", framework="redis",
            language="conf", description="cache",
            workspace_name="cache", port=6379, depends_on=[],
        )
        arch = SystemArchitecture(
            project_name="P", project_slug="p", description="",
            services=[_db(), cache, _backend()],
        )
        dispatcher.run(architecture=arch, enriched_spec=_spec())
        # Only backend was planned (db has language=sql, cache=conf)
        assert planner_called == ["backend"]

    def test_typescript_service_is_planned(self):
        planner_called: list = []

        def planner_factory(service):
            planner_called.append(service.name)
            planner = MagicMock()
            planner.plan_service.return_value = []
            return planner

        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=planner_factory,
            coder_factory=_coder_factory_with([]),
            progression_factory=_progression_factory(),
        )
        frontend = ServiceDefinition(
            name="frontend", service_type="frontend", framework="react",
            language="typescript", description="ui",
            workspace_name="frontend", port=5173, depends_on=[],
        )
        arch = SystemArchitecture(
            project_name="P", project_slug="p", description="",
            services=[frontend],
        )
        dispatcher.run(architecture=arch, enriched_spec=_spec())
        assert planner_called == ["frontend"]


# ── Repair ─────────────────────────────────────────────────────────────


class TestRepair:
    def test_repair_runs_planner_then_dispatches_fix_issues(self, tmp_path):
        """End-to-end repair: store has prior issues, repair() reads
        them, calls plan_repair, dispatches the fix-issues, store ends
        up with originals + fix-issues."""
        from bizniz.code_reviewer.types import (
            CodeReviewReport, FlaggedSymbol,
        )
        from bizniz.coder.types import CoderResult
        from bizniz.project.project import Project
        from bizniz.state.issue_store import IssueStateStore

        # Real store on a real (tmp) DB.
        project = Project(root=tmp_path, project_name="t")
        store = IssueStateStore(db=project.db, job_id="J1", milestone_index=1)

        # Seed: one prior passing issue.
        store.record_planned("backend", [_coder_issue("BE-001")])
        store.mark_finished(
            "backend", "BE-001", status="passed",
            result=CoderResult(issue_id="BE-001", status="passed"),
        )

        # ServicePlanner emits one fix-issue when called for repair.
        def planner_factory(_service):
            planner = MagicMock()
            planner.plan_repair.return_value = [
                _coder_issue("BE-001-fix1"),
            ]
            return planner

        # Coder for the fix returns passed.
        coder = MagicMock()
        coder.code_issue.return_value = CoderResult(
            issue_id="BE-001-fix1", status="passed",
        )

        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=planner_factory,
            coder_factory=lambda model, service: coder,
            progression_factory=_progression_factory(),
        )

        review = CodeReviewReport(
            milestone_name="M1", approved=False,
            flagged_symbols=[FlaggedSymbol(
                file="backend/app/users.py", line=12,
                symbol="get_current_user_with_roles",
                kind="import", reason="x", severity="critical",
            )],
        )

        result = dispatcher.repair(
            architecture=_arch(services=[_backend()]),
            enriched_spec=_spec(),
            coverage_report=None,
            code_review_report=review,
            repair_iteration=1,
            issue_store=store,
        )
        # EngineerResult includes both BE-001 (passed originally) and
        # BE-001-fix1 (passed in repair).
        ids = {i.id for i in result.plan.issues}
        assert ids == {"BE-001", "BE-001-fix1"}
        assert "BE-001-fix1" in result.completed_issue_ids

    def test_repair_skips_services_with_no_prior_issues(self, tmp_path):
        from bizniz.coder.types import CoderResult
        from bizniz.project.project import Project
        from bizniz.state.issue_store import IssueStateStore

        project = Project(root=tmp_path, project_name="t")
        store = IssueStateStore(db=project.db, job_id="J1", milestone_index=1)

        # No prior issues for backend or db
        planner_factory_called = []

        def planner_factory(service):
            planner_factory_called.append(service.name)
            planner = MagicMock()
            planner.plan_repair.return_value = []
            return planner

        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=planner_factory,
            coder_factory=_coder_factory_with([]),
            progression_factory=_progression_factory(),
        )
        dispatcher.repair(
            architecture=_arch(),
            enriched_spec=_spec(),
            coverage_report=None,
            code_review_report=None,
            repair_iteration=1,
            issue_store=store,
        )
        # No services touched the planner since all skip on empty store
        assert planner_factory_called == []

    def test_repair_requires_store(self):
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({}),
            coder_factory=_coder_factory_with([]),
            progression_factory=_progression_factory(),
        )
        with pytest.raises(RuntimeError, match="IssueStateStore"):
            dispatcher.repair(
                architecture=_arch(),
                enriched_spec=_spec(),
                coverage_report=None,
                code_review_report=None,
                repair_iteration=1,
            )
