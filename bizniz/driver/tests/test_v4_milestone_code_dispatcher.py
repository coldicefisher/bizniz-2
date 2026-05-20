"""Tests for ``V4MilestoneCodeDispatcher``.

Mocks the LLM-calling agents (CoderTesterAgent + ServicePlannerWith
Scaffold). Verifies the wiring: planner→PIRunner→per-issue dispatch
→ ValidatedIssue → EngineerResult.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.coder.types import Issue as CoderIssue
from bizniz.coder_tester.types import CoderTesterResult, FilledFile
from bizniz.driver.v4_milestone_code_dispatcher import (
    V4MilestoneCodeDispatcher, _is_code_bearing,
)
from bizniz.per_issue_validator.types import ValidatedIssue
from bizniz.quality_engineer.types import CapabilitySpec, EnrichedSpec
from bizniz.service_planner.scaffolded import (
    ScaffoldedPlanResult, SeededFile,
)


# ── Fixtures ────────────────────────────────────────────────────────


def _service(name="backend", language="python") -> ServiceDefinition:
    return ServiceDefinition(
        name=name,
        service_type="backend",
        framework="fastapi",
        language=language,
        workspace_name=name,
        port=8000,
        description=f"{name} service",
        depends_on=[],
    )


def _arch(services=None) -> SystemArchitecture:
    return SystemArchitecture(
        project_name="test",
        project_slug="test",
        description="test",
        services=services or [_service()],
    )


def _spec() -> EnrichedSpec:
    return EnrichedSpec(
        milestone_name="M1",
        capabilities=[
            CapabilitySpec(id="cap_a", name="A", description=""),
        ],
    )


def _ws_factory(tmp_path):
    """Build a workspace-for-service callable rooted under tmp_path."""
    from bizniz.workspace.local_workspace import LocalWorkspace

    def _factory(name: str):
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        return LocalWorkspace(root)
    return _factory


def _issue(iid: str, target=None, test=None, deps=None) -> CoderIssue:
    return CoderIssue(
        id=iid,
        title=iid,
        description=iid,
        service="backend",
        language="python",
        target_files=target or [f"app/{iid.lower()}.py"],
        test_files=test or [f"tests/test_{iid.lower()}.py"],
        success_criteria=[],
        spec_refs=["cap_a"],
        depends_on=deps or [],
    )


# ── Helpers ────────────────────────────────────────────────────────


class TestIsCodeBearing:
    def test_python_is_code_bearing(self):
        assert _is_code_bearing(_service(language="python")) is True

    def test_yaml_is_not_code_bearing(self):
        assert _is_code_bearing(_service(language="yaml")) is False

    def test_sql_is_not_code_bearing(self):
        assert _is_code_bearing(_service(language="sql")) is False


# ── IMPLEMENT path ─────────────────────────────────────────────────


class TestImplementPath:
    def test_planner_then_pirunner_then_engineer_result(self, tmp_path):
        # Mock planner returns 2 issues + 2 seeded files.
        issues = [_issue("BE-001"), _issue("BE-002")]
        seeded = [
            SeededFile(path="app/be-001.py", content="pass\n", rationale=""),
            SeededFile(path="app/be-002.py", content="pass\n", rationale=""),
        ]
        plan_result = ScaffoldedPlanResult(
            issues=issues, seeded_files=seeded,
        )
        planner = MagicMock()
        planner.plan_service.return_value = plan_result

        # Mock agent returns clean code for each issue.
        agent = MagicMock()

        def code_issue(*, issue, **kwargs):
            return CoderTesterResult(
                issue_id=issue.id,
                filled_files=[
                    FilledFile(
                        path=issue.target_files[0],
                        content="def x(): return 1\n",
                        role="code",
                    ),
                    FilledFile(
                        path=issue.test_files[0],
                        content="def test_x(): assert True\n",
                        role="test",
                    ),
                ],
            )
        agent.code_issue.side_effect = code_issue

        dispatcher = V4MilestoneCodeDispatcher(
            planner_factory=lambda _s: planner,
            coder_tester_factory=lambda _s: agent,
            workspace_for_service=_ws_factory(tmp_path),
            max_parallel_coders=4,
        )
        result = dispatcher.run(
            architecture=_arch(),
            enriched_spec=_spec(),
        )
        # Both issues completed.
        assert sorted(result.completed_issue_ids) == ["BE-001", "BE-002"]
        assert result.deferred_issue_ids == []
        assert result.final_test_status == "passed"
        # Both planner + 2 agent calls.
        assert planner.plan_service.call_count == 1
        assert agent.code_issue.call_count == 2

    def test_planner_failure_returns_partial_result(self, tmp_path):
        planner = MagicMock()
        planner.plan_service.side_effect = RuntimeError("planner blew up")

        dispatcher = V4MilestoneCodeDispatcher(
            planner_factory=lambda _s: planner,
            coder_tester_factory=lambda _s: MagicMock(),
            workspace_for_service=_ws_factory(tmp_path),
        )
        result = dispatcher.run(
            architecture=_arch(),
            enriched_spec=_spec(),
        )
        # No issues completed, no issues deferred (planner never emitted any).
        assert result.completed_issue_ids == []
        assert result.deferred_issue_ids == []
        assert "planner failed" in (result.notes[0] if result.notes else "")

    def test_skips_infrastructure_services(self, tmp_path):
        # Two services: backend (python) + db (sql). Only backend gets dispatched.
        db = _service("db", language="sql")
        backend = _service("backend", language="python")
        planner = MagicMock()
        planner.plan_service.return_value = ScaffoldedPlanResult(
            issues=[_issue("BE-001")],
            seeded_files=[
                SeededFile(path="app/be-001.py", content="pass\n", rationale=""),
            ],
        )
        agent = MagicMock()
        agent.code_issue.return_value = CoderTesterResult(
            issue_id="BE-001",
            filled_files=[
                FilledFile(
                    path="app/be-001.py",
                    content="def x(): return 1\n", role="code",
                ),
                FilledFile(
                    path="tests/test_be-001.py",
                    content="def test_x(): assert True\n", role="test",
                ),
            ],
        )

        dispatcher = V4MilestoneCodeDispatcher(
            planner_factory=lambda _s: planner,
            coder_tester_factory=lambda _s: agent,
            workspace_for_service=_ws_factory(tmp_path),
        )
        # arch has both; only backend is planned.
        dispatcher.run(
            architecture=_arch(services=[backend, db]),
            enriched_spec=_spec(),
        )
        assert planner.plan_service.call_count == 1
        assert (
            planner.plan_service.call_args.kwargs["service"].name == "backend"
        )

    def test_only_service_filter(self, tmp_path):
        # When only_service is set, other services skipped entirely.
        backend = _service("backend", language="python")
        worker = _service("worker", language="python")
        planner = MagicMock()
        planner.plan_service.return_value = ScaffoldedPlanResult(
            issues=[_issue("X-001")],
            seeded_files=[
                SeededFile(path="app/x-001.py", content="pass\n", rationale=""),
            ],
        )
        agent = MagicMock()
        agent.code_issue.return_value = CoderTesterResult(
            issue_id="X-001",
            filled_files=[
                FilledFile(
                    path="app/x-001.py",
                    content="def x(): return 1\n", role="code",
                ),
                FilledFile(
                    path="tests/test_x-001.py",
                    content="def t(): assert True\n", role="test",
                ),
            ],
        )

        dispatcher = V4MilestoneCodeDispatcher(
            planner_factory=lambda _s: planner,
            coder_tester_factory=lambda _s: agent,
            workspace_for_service=_ws_factory(tmp_path),
            only_service="worker",
        )
        dispatcher.run(
            architecture=_arch(services=[backend, worker]),
            enriched_spec=_spec(),
        )
        # Only worker planned.
        assert planner.plan_service.call_count == 1
        assert (
            planner.plan_service.call_args.kwargs["service"].name == "worker"
        )


# ── REPAIR path ────────────────────────────────────────────────────


class TestRepairPath:
    def test_repair_without_planner_factory_raises(self, tmp_path):
        dispatcher = V4MilestoneCodeDispatcher(
            planner_factory=lambda _s: MagicMock(),
            coder_tester_factory=lambda _s: MagicMock(),
            workspace_for_service=_ws_factory(tmp_path),
            repair_planner_factory=None,
        )
        with pytest.raises(RuntimeError, match="no repair_planner_factory"):
            dispatcher.repair(
                architecture=_arch(),
                enriched_spec=_spec(),
                coverage_report=MagicMock(),
                code_review_report=MagicMock(),
                repair_iteration=1,
            )

    def test_repair_dispatches_fix_issues_via_repair_factory(self, tmp_path):
        # Repair planner returns 1 fix-issue via plan_repair (production
        # ServicePlanner contract: returns List[Issue] directly).
        fix_issue = _issue("BE-fix1")

        repair_planner = MagicMock()
        repair_planner.plan_repair.return_value = [fix_issue]

        # Repair agent factory tracked separately from implement.
        implement_agent = MagicMock()
        repair_agent = MagicMock()
        repair_agent.code_issue.return_value = CoderTesterResult(
            issue_id="BE-fix1",
            filled_files=[
                FilledFile(
                    path="app/be-fix1.py",
                    content="def x(): return 1\n", role="code",
                ),
                FilledFile(
                    path="tests/test_be-fix1.py",
                    content="def test_x(): assert True\n", role="test",
                ),
            ],
        )

        dispatcher = V4MilestoneCodeDispatcher(
            planner_factory=lambda _s: MagicMock(),
            coder_tester_factory=lambda _s: implement_agent,
            repair_coder_tester_factory=lambda _s: repair_agent,
            workspace_for_service=_ws_factory(tmp_path),
            repair_planner_factory=lambda _s: repair_planner,
        )
        result = dispatcher.repair(
            architecture=_arch(),
            enriched_spec=_spec(),
            coverage_report=MagicMock(),
            code_review_report=MagicMock(),
            repair_iteration=1,
        )
        # Fix-issue completed, dispatched via REPAIR tier (not IMPLEMENT).
        assert result.completed_issue_ids == ["BE-fix1"]
        repair_agent.code_issue.assert_called()
        implement_agent.code_issue.assert_not_called()

    def test_parallel_services_dispatched_concurrently(self, tmp_path):
        """v4 fix #2: two services in the same topological layer
        run concurrently (not sequentially)."""
        import threading
        import time as _time
        from bizniz.workspace.local_workspace import LocalWorkspace

        backend = _service("backend", language="python")
        frontend = _service("frontend", language="python")

        planner = MagicMock()
        planner.plan_service.return_value = ScaffoldedPlanResult(
            issues=[_issue("X-001")],
            seeded_files=[
                SeededFile(path="app/x-001.py", content="pass\n", rationale=""),
            ],
        )

        in_flight = []
        max_in_flight = [0]
        lock = threading.Lock()

        def slow_code_issue(*, issue, **kwargs):
            with lock:
                in_flight.append(issue.id)
                if len(in_flight) > max_in_flight[0]:
                    max_in_flight[0] = len(in_flight)
            _time.sleep(0.1)
            with lock:
                in_flight.remove(issue.id)
            return CoderTesterResult(
                issue_id=issue.id,
                filled_files=[
                    FilledFile(
                        path=issue.target_files[0],
                        content="def x(): return 1\n", role="code",
                    ),
                    FilledFile(
                        path=issue.test_files[0],
                        content="def test_x(): assert True\n", role="test",
                    ),
                ],
            )

        agent = MagicMock()
        agent.code_issue.side_effect = slow_code_issue

        # Per-service factory returns the same mock so we can observe
        # max-in-flight across services.
        ws_factory = _ws_factory(tmp_path)
        dispatcher = V4MilestoneCodeDispatcher(
            planner_factory=lambda _s: planner,
            coder_tester_factory=lambda _s: agent,
            workspace_for_service=ws_factory,
        )
        t0 = _time.time()
        dispatcher.run(
            architecture=_arch(services=[backend, frontend]),
            enriched_spec=_spec(),
        )
        wall = _time.time() - t0
        # Both services in same layer (no cross-service deps) →
        # ran in parallel → wall ≈ max(per-service), not sum.
        # 2 services × 1 issue × 0.1s/issue: parallel = ~0.1-0.2s,
        # sequential = ~0.2-0.4s. Use 0.3s as the boundary.
        assert wall < 0.6, f"expected parallel, got {wall:.2f}s"
        # At least 2 issues in flight simultaneously across services.
        assert max_in_flight[0] >= 2

    def test_repair_workspace_summary_passed_to_planner_when_supported(self, tmp_path):
        """v4 fix #4: planner.plan_repair is called with
        workspace_summary kwarg when the planner's signature accepts
        it AND there's meaningful workspace content."""
        # Set up a workspace with a .py file so _compute_workspace_summary
        # has something to report.
        ws_factory = _ws_factory(tmp_path)
        ws = ws_factory("backend")
        ws.write_file("app/existing.py", "def foo(): pass\n")

        # Mock planner whose plan_repair signature accepts workspace_summary.
        def plan_repair_stub(*, architecture, enriched_spec, service,
                             prior_issues, prior_dispositions,
                             coverage_report, code_review_report,
                             repair_iteration, skeleton_md=None,
                             auth_contract=None, workspace_summary=None):
            return []
        repair_planner = MagicMock()
        repair_planner.plan_repair.side_effect = plan_repair_stub

        dispatcher = V4MilestoneCodeDispatcher(
            planner_factory=lambda _s: MagicMock(),
            coder_tester_factory=lambda _s: MagicMock(),
            workspace_for_service=ws_factory,
            repair_planner_factory=lambda _s: repair_planner,
        )
        dispatcher.repair(
            architecture=_arch(),
            enriched_spec=_spec(),
            coverage_report=MagicMock(),
            code_review_report=MagicMock(),
            repair_iteration=1,
        )
        assert repair_planner.plan_repair.called
        kwargs = repair_planner.plan_repair.call_args.kwargs
        # workspace_summary is passed when computed AND planner accepts it.
        assert "workspace_summary" in kwargs
        assert kwargs["workspace_summary"]  # non-empty
        assert "app/existing.py" in kwargs["workspace_summary"]

    def test_repair_planner_emits_zero_issues_logs_warning(self, tmp_path):
        # Empty repair plan → service skipped, no fix-issues dispatched.
        repair_planner = MagicMock()
        repair_planner.plan_repair.return_value = []
        repair_agent = MagicMock()

        dispatcher = V4MilestoneCodeDispatcher(
            planner_factory=lambda _s: MagicMock(),
            coder_tester_factory=lambda _s: MagicMock(),
            repair_coder_tester_factory=lambda _s: repair_agent,
            workspace_for_service=_ws_factory(tmp_path),
            repair_planner_factory=lambda _s: repair_planner,
        )
        result = dispatcher.repair(
            architecture=_arch(),
            enriched_spec=_spec(),
            coverage_report=MagicMock(),
            code_review_report=MagicMock(),
            repair_iteration=1,
        )
        assert result.completed_issue_ids == []
        assert result.deferred_issue_ids == []
        repair_agent.code_issue.assert_not_called()
