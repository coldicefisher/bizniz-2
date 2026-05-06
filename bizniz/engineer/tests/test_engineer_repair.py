"""Tests for Engineer.repair() — mode-aware fix flow."""
import json
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.code_reviewer.types import (
    CodeReviewReport,
    FlaggedSymbol,
    AntiPatternViolation,
)
from bizniz.engineer.agent import Engineer
from bizniz.engineer.prompts.system_prompt import (
    ENGINEER_REPAIR_SYSTEM_PROMPT,
    ENGINEER_SYSTEM_PROMPT,
)
from bizniz.engineer.types import EngineerResult
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.types import (
    CapabilitySpec,
    EnrichedSpec,
)
from bizniz.workspace.local_workspace import LocalWorkspace


# ── Fixtures ──────────────────────────────────────────────────────────


def _arch():
    return SystemArchitecture(
        project_name="X", project_slug="x", description="d",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="API",
                workspace_name="backend", port=8000,
            ),
        ],
    )


def _milestone():
    return Milestone(
        sequence_index=1, name="Pet CRUD",
        problem_slice="CRUD pets.",
    )


def _spec():
    return EnrichedSpec(
        milestone_name="Pet CRUD",
        capabilities=[
            CapabilitySpec(
                id="create_pet", name="N", description="d",
                inputs=[], outputs=[], validation_rules=[], error_cases=[],
                edge_cases=[], auth_required=True, allowed_roles=[],
                test_scenarios=[],
            ),
        ],
    )


def _report(critical=True):
    return CodeReviewReport(
        milestone_name="Pet CRUD",
        approved=False,
        flagged_symbols=[FlaggedSymbol(
            file="app/api/pets.py", line=12,
            symbol="UnknownThing", kind="import",
            reason="fabricated import; use real fastapi.APIRouter",
            severity="critical" if critical else "warning",
        )],
        anti_pattern_violations=[AntiPatternViolation(
            file="app/api/auth.py", line=20,
            anti_pattern="never log raw passwords",
            evidence="logger.info(password)",
            severity="critical" if critical else "warning",
        )],
        summary="Two issues to fix.",
    )


def _action(action_type: str, **kw) -> str:
    base = {
        "thinking": "x", "action": action_type,
        "approach": "", "issues": [],
        "path": "", "new_content": "", "query": "", "service": "",
        "url": "", "request_data": "", "command": "", "sql": "", "token": "",
        "summary": "", "final_test_status": "not_run",
        "completed_issue_ids": [], "deferred_issue_ids": [], "notes": [],
    }
    base.update(kw)
    return json.dumps(base)


def _issue(id="I1", title="t", desc="d", target=None, tests=None,
           spec_refs=(), depends_on=()):
    return {
        "id": id, "title": title, "description": desc,
        "target_files": list(target or []),
        "test_files": list(tests or []),
        "success_criteria": [],
        "spec_refs": list(spec_refs),
        "depends_on": list(depends_on),
    }


def _client_with_actions(actions: list):
    client = MagicMock(spec=BaseAIClient)
    client.get_text.side_effect = [(a, "j", []) for a in actions]
    return client


def _engineer(client, tmp_path):
    return Engineer(
        client=client, workspace=LocalWorkspace(root=tmp_path),
        compose_path="/p/proj/compose.yml", target_service="backend",
        tool_iterations=10, timeout_seconds=10,
    )


# ── Mode wiring ─────────────────────────────────────────────────────────


class TestModeSelection:
    def test_implement_uses_implement_prompt(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok",
                    issues=[_issue(spec_refs=("create_pet",))]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        eng.implement(_milestone(), _arch(), _spec())
        first_msgs = eng._client.get_text.call_args_list[0].kwargs["messages"]
        sys = next(m["content"] for m in first_msgs if m["role"] == "system")
        assert sys == ENGINEER_SYSTEM_PROMPT

    def test_repair_uses_repair_prompt(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="passed",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        eng.repair(_milestone(), _arch(), _report(), enriched_spec=_spec())
        first_msgs = eng._client.get_text.call_args_list[0].kwargs["messages"]
        sys = next(m["content"] for m in first_msgs if m["role"] == "system")
        assert sys == ENGINEER_REPAIR_SYSTEM_PROMPT


# ── spec_refs relaxation in repair mode ────────────────────────────────


class TestSpecRefsRelaxation:
    def test_repair_accepts_issue_without_spec_refs(self, tmp_path):
        # Implement mode rejects this; repair accepts it.
        actions = [
            _action("submit_plan", approach="ok",
                    issues=[_issue(spec_refs=())]),
            _action("submit_implementation", summary="fixed",
                    final_test_status="passed",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.repair(_milestone(), _arch(), _report())
        assert isinstance(result, EngineerResult)
        assert result.plan.issues[0].spec_refs == []

    def test_implement_still_rejects_no_spec_refs(self, tmp_path):
        # Sanity: implement-mode invariant still enforced.
        actions = [
            _action("submit_plan", approach="ok",
                    issues=[_issue(spec_refs=())]),
            _action("submit_plan", approach="ok",
                    issues=[_issue(spec_refs=("create_pet",))]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        # First was rejected; second accepted.
        assert result.plan.issues[0].spec_refs == ["create_pet"]


# ── Initial context threading ─────────────────────────────────────────


class TestRepairContext:
    def test_threads_review_report(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="passed",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        eng.repair(_milestone(), _arch(), _report())
        first = eng._client.get_text.call_args_list[0].kwargs["messages"]
        user = next(m["content"] for m in first if m["role"] == "user")
        assert "Code Review Report" in user
        assert "UnknownThing" in user
        assert "fabricated import" in user
        assert "never log raw passwords" in user

    def test_repair_summary_counts(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="passed",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        eng.repair(_milestone(), _arch(), _report(critical=True))
        first = eng._client.get_text.call_args_list[0].kwargs["messages"]
        user = next(m["content"] for m in first if m["role"] == "user")
        # Total findings = 2 (1 flagged + 1 anti-pattern); critical = 2.
        assert "2 finding(s) total, 2 critical" in user

    def test_threads_optional_enriched_spec(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="passed",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        eng.repair(_milestone(), _arch(), _report(), enriched_spec=_spec())
        first = eng._client.get_text.call_args_list[0].kwargs["messages"]
        user = next(m["content"] for m in first if m["role"] == "user")
        assert "EnrichedSpec" in user
        assert "create_pet" in user

    def test_skips_enriched_spec_when_omitted(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="passed",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        eng.repair(_milestone(), _arch(), _report())
        first = eng._client.get_text.call_args_list[0].kwargs["messages"]
        user = next(m["content"] for m in first if m["role"] == "user")
        assert "EnrichedSpec" not in user

    def test_threads_auth_contract(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="passed",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        eng.repair(
            _milestone(), _arch(), _report(),
            auth_contract="role: groomer",
        )
        first = eng._client.get_text.call_args_list[0].kwargs["messages"]
        user = next(m["content"] for m in first if m["role"] == "user")
        assert "Auth contract" in user
        assert "groomer" in user


# ── Plan-first invariant in repair mode ────────────────────────────────


class TestPlanFirstInRepair:
    def test_write_file_rejected_before_plan_in_repair(self, tmp_path):
        actions = [
            _action("write_file", path="x.py", new_content="y"),
            _action("submit_plan", approach="ok", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="passed",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        eng.repair(_milestone(), _arch(), _report())
        # First action gated; file should not exist.
        assert not (tmp_path / "x.py").exists()


# ── Mode resets cleanly between calls ──────────────────────────────────


class TestModeReset:
    def test_repair_then_implement_resets_mode(self, tmp_path):
        # First: repair
        actions_repair = [
            _action("submit_plan", approach="ok", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="passed",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions_repair), tmp_path)
        eng.repair(_milestone(), _arch(), _report())
        assert eng._mode == "repair"
        # Second: implement (with NEW client mocking)
        actions_impl = [
            _action("submit_plan", approach="ok",
                    issues=[_issue(spec_refs=("create_pet",))]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng._client = _client_with_actions(actions_impl)
        eng.implement(_milestone(), _arch(), _spec())
        assert eng._mode == "implement"

    def test_plan_resets_between_calls(self, tmp_path):
        actions1 = [
            _action("submit_plan", approach="first", issues=[_issue("I1")]),
            _action("submit_implementation", summary="s",
                    final_test_status="passed",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions1), tmp_path)
        eng.repair(_milestone(), _arch(), _report())
        # Now repair again — must get a fresh plan slot.
        actions2 = [
            _action("submit_plan", approach="second", issues=[_issue("I2")]),
            _action("submit_implementation", summary="s",
                    final_test_status="passed",
                    completed_issue_ids=["I2"]),
        ]
        eng._client = _client_with_actions(actions2)
        result = eng.repair(_milestone(), _arch(), _report())
        assert result.plan.approach == "second"
        assert result.plan.issues[0].id == "I2"
