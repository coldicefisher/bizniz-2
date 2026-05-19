"""Unit tests for ``V3MilestoneCodeDispatcher``.

Scope: structural / wiring tests, not end-to-end LLM dispatch.
Real CoderAgentV3 + ServicePlannerWithScaffold quality is validated
by the perf-test scenarios (Phase 1 + 2a + 2c). These tests just lock
in:

  1. The dispatcher delegates ``.repair()`` to the injected v2 fallback
     (Stage A safety: keeps review_repair working without rewriting it).
  2. ``.run()`` returns a valid EngineerResult shape with synthesized
     ``EngineerPlan.approach`` so downstream phases don't crash on
     missing fields.
  3. Infrastructure-only services (yaml/sql) get skipped same as v2.
"""
from __future__ import annotations

from typing import List
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.coder.agent_v3 import CoderAgentV3Result, FilledFile
from bizniz.coder.types import Issue as CoderIssue
from bizniz.driver.v3_milestone_code_dispatcher import (
    V3MilestoneCodeDispatcher, _is_code_bearing,
)
from bizniz.engineer.types import EngineerResult
from bizniz.quality_engineer.types import EnrichedSpec
from bizniz.service_planner.scaffolded import ScaffoldedPlanResult, SeededFile


# ── Fixtures ──────────────────────────────────────────────────────


def _backend_service() -> ServiceDefinition:
    return ServiceDefinition(
        name="backend", service_type="backend",
        framework="fastapi", language="python",
        description="API", workspace_name="backend", port=8000,
    )


def _db_service() -> ServiceDefinition:
    return ServiceDefinition(
        name="db", service_type="database",
        framework="postgres", language="sql",
        description="db", workspace_name="db", port=5432,
    )


def _arch(*services) -> SystemArchitecture:
    return SystemArchitecture(
        project_name="X", project_slug="x", description="x",
        services=list(services),
    )


def _spec() -> EnrichedSpec:
    return EnrichedSpec(milestone_name="M1", capabilities=[])


def _coder_issue(iid: str, target: str) -> CoderIssue:
    return CoderIssue(
        id=iid, title=f"Issue {iid}",
        description=f"do work for {iid}",
        service="backend", language="python",
        target_files=[target], test_files=[],
        success_criteria=[], spec_refs=[], depends_on=[],
    )


# ── Tests ─────────────────────────────────────────────────────────


class TestCodeBearing:
    def test_python_is_code_bearing(self):
        assert _is_code_bearing(_backend_service()) is True

    def test_sql_is_not_code_bearing(self):
        assert _is_code_bearing(_db_service()) is False


class TestRepairDelegation:
    """Stage A safety: dispatcher must forward .repair() to the
    injected v2 fallback. Without this, MilestoneLoop's review_repair
    phase would crash with AttributeError after IMPLEMENT succeeds."""

    def test_repair_delegates_to_injected_v2(self, tmp_path):
        v2 = MagicMock()
        v2.repair.return_value = "v2-result"
        d = V3MilestoneCodeDispatcher(
            planner_factory=lambda s: MagicMock(),
            coder_factory=lambda s: MagicMock(),
            workspace_for_service=lambda name: MagicMock(root=str(tmp_path)),
            repair_dispatcher=v2,
        )
        out = d.repair(architecture="A", enriched_spec="S", coverage_report="C")
        assert out == "v2-result"
        v2.repair.assert_called_once()

    def test_repair_without_dispatcher_raises_clearly(self, tmp_path):
        d = V3MilestoneCodeDispatcher(
            planner_factory=lambda s: MagicMock(),
            coder_factory=lambda s: MagicMock(),
            workspace_for_service=lambda name: MagicMock(root=str(tmp_path)),
            repair_dispatcher=None,
        )
        with pytest.raises(RuntimeError, match="no v2 repair_dispatcher"):
            d.repair(architecture="A")


class TestRunSkipsInfrastructure:
    def test_db_service_is_skipped(self, tmp_path):
        d = V3MilestoneCodeDispatcher(
            planner_factory=lambda s: MagicMock(),
            coder_factory=lambda s: MagicMock(),
            workspace_for_service=lambda name: MagicMock(root=str(tmp_path / name)),
        )
        result = d.run(
            architecture=_arch(_db_service()),
            enriched_spec=_spec(),
        )
        # Plan exists, no issues (infrastructure skipped).
        assert isinstance(result, EngineerResult)
        assert result.plan.issues == []
        assert result.completed_issue_ids == []
        # Stage A note: when nothing dispatched, final_test_status is
        # "not_run" (no code work means no test outcome).
        assert result.final_test_status == "not_run"


class TestRunHappyPath:
    """End-to-end ``.run()`` with mocked planner + coder factories.
    Locks in the contract that the dispatcher: plans, materializes
    seeded files, invokes the coder, marks issues completed based on
    target_files coverage in the filled output."""

    def test_full_dispatch_returns_completed_engineer_result(self, tmp_path):
        # Mock the planner — emits 2 issues + 2 seeded files.
        issues = [
            _coder_issue("BE-001", "app/routes/me.py"),
            _coder_issue("BE-002", "app/schemas/me.py"),
        ]
        seeded = [
            SeededFile(path="app/routes/me.py", content="# seed\npass\n",
                       rationale="filled by BE-001"),
            SeededFile(path="app/schemas/me.py", content="# seed\npass\n",
                       rationale="filled by BE-002"),
        ]
        plan_result = ScaffoldedPlanResult(issues=issues, seeded_files=seeded)
        planner = MagicMock()
        planner.plan_service.return_value = plan_result

        # Mock the coder — fills both files.
        filled = [
            FilledFile(path="app/routes/me.py", content="# filled\nrouter = None\n"),
            FilledFile(path="app/schemas/me.py", content="# filled\nclass Me: pass\n"),
        ]
        fill_result = CoderAgentV3Result(filled_files=filled)
        coder = MagicMock()
        coder.fill_milestone.return_value = fill_result

        backend = _backend_service()
        d = V3MilestoneCodeDispatcher(
            planner_factory=lambda s: planner,
            coder_factory=lambda s: coder,
            workspace_for_service=lambda name: MagicMock(root=str(tmp_path / name)),
        )
        result = d.run(
            architecture=_arch(backend),
            enriched_spec=_spec(),
        )

        # Both issues completed.
        assert sorted(result.completed_issue_ids) == ["BE-001", "BE-002"]
        assert result.deferred_issue_ids == []
        assert result.final_test_status == "passed"
        # EngineerPlan has the synthesized approach + the issues.
        assert "v3 IMPLEMENT" in result.plan.approach
        assert len(result.plan.issues) == 2

        # Seed + filled files both written to workspace.
        ws_root = tmp_path / "backend"
        assert (ws_root / "app/routes/me.py").read_text().strip() == "# filled\nrouter = None"
        assert (ws_root / "app/schemas/me.py").read_text().startswith("# filled")

    def test_partial_coverage_marks_deferred(self, tmp_path):
        # Coder filled only ONE of the two issue's target files —
        # the other issue should be deferred.
        issues = [
            _coder_issue("BE-001", "app/routes/a.py"),
            _coder_issue("BE-002", "app/routes/b.py"),
        ]
        seeded = [
            SeededFile(path="app/routes/a.py", content="# seed\npass\n",
                       rationale="filled by BE-001"),
            SeededFile(path="app/routes/b.py", content="# seed\npass\n",
                       rationale="filled by BE-002"),
        ]
        plan_result = ScaffoldedPlanResult(issues=issues, seeded_files=seeded)
        planner = MagicMock()
        planner.plan_service.return_value = plan_result

        # Coder fills only a.py.
        fill_result = CoderAgentV3Result(filled_files=[
            FilledFile(path="app/routes/a.py", content="# filled\n"),
        ])
        coder = MagicMock()
        coder.fill_milestone.return_value = fill_result

        backend = _backend_service()
        d = V3MilestoneCodeDispatcher(
            planner_factory=lambda s: planner,
            coder_factory=lambda s: coder,
            workspace_for_service=lambda name: MagicMock(root=str(tmp_path / name)),
        )
        result = d.run(
            architecture=_arch(backend),
            enriched_spec=_spec(),
        )

        assert result.completed_issue_ids == ["BE-001"]
        assert result.deferred_issue_ids == ["BE-002"]
        assert result.final_test_status == "partial"
