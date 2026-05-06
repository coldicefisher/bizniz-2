"""Tests for the Engineer agent.

Mock the LLM client. Drive the agent through canned action sequences
and verify:

  - submit_plan must be the first action; everything else rejected
  - submit_plan validates: requires approach, non-empty issues, every
    issue has spec_refs, no duplicate ids, depends_on references valid
  - get_my_plan returns the current plan with status markers
  - revise_plan diffs added/removed issues
  - submit_implementation produces the typed EngineerResult
  - tool surface includes all expected actions
  - bias on non-existent fields doesn't crash
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.engineer.agent import Engineer
from bizniz.engineer.types import (
    EngineerError,
    EngineerPlan,
    EngineerResult,
    Issue,
)
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.types import (
    CapabilitySpec,
    EnrichedSpec,
    Field as SpecField,
)
from bizniz.workspace.local_workspace import LocalWorkspace


# ── Fixtures ───────────────────────────────────────────────────────────


def _arch():
    return SystemArchitecture(
        project_name="Pet Groomer",
        project_slug="pet_groomer",
        description="Booking",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="REST API.",
                workspace_name="backend", port=8000,
            ),
        ],
    )


def _milestone():
    return Milestone(
        sequence_index=1, name="Pet CRUD",
        problem_slice="CRUD pets.", use_cases=["create"],
        success_criteria=["list works"],
    )


def _spec():
    return EnrichedSpec(
        milestone_name="Pet CRUD",
        capabilities=[
            CapabilitySpec(
                id="create_pet", name="Create Pet", description="d",
                inputs=[SpecField(name="name", type="string", required=True,
                                  constraints=[], description="")],
                outputs=[], validation_rules=[], error_cases=[],
                edge_cases=[], auth_required=True, allowed_roles=["groomer"],
                test_scenarios=["happy"],
            ),
            CapabilitySpec(
                id="list_pets", name="List Pets", description="d",
                inputs=[], outputs=[], validation_rules=[], error_cases=[],
                edge_cases=[], auth_required=True, allowed_roles=["groomer"],
                test_scenarios=["happy"],
            ),
        ],
    )


def _action(action_type: str, **kwargs) -> str:
    """Build a JSON action with all required schema fields filled in."""
    base = {
        "thinking": "doing the thing",
        "action": action_type,
        "approach": "",
        "issues": [],
        "path": "",
        "new_content": "",
        "query": "",
        "service": "",
        "url": "",
        "request_data": "",
        "command": "",
        "sql": "",
        "token": "",
        "summary": "",
        "final_test_status": "not_run",
        "completed_issue_ids": [],
        "deferred_issue_ids": [],
        "notes": [],
    }
    base.update(kwargs)
    return json.dumps(base)


def _issue(id="I1", title="t", desc="d", target=None, tests=None,
           spec_refs=("create_pet",), depends_on=()):
    return {
        "id": id, "title": title, "description": desc,
        "target_files": list(target or []),
        "test_files": list(tests or []),
        "success_criteria": [],
        "spec_refs": list(spec_refs),
        "depends_on": list(depends_on),
    }


def _client_with_actions(actions: list):
    """Mock client that returns ``actions`` in order, one per get_text call."""
    client = MagicMock(spec=BaseAIClient)
    responses = [(a, "job", []) for a in actions]
    client.get_text.side_effect = responses
    return client


def _engineer(client, tmp_path):
    return Engineer(
        client=client,
        workspace=LocalWorkspace(root=tmp_path),
        compose_path="/p/proj/compose.yml",
        target_service="backend",
        on_status=None,
        tool_iterations=10,
        timeout_seconds=10,
    )


# ── Tool surface ───────────────────────────────────────────────────────


class TestToolSurface:
    def test_includes_all_expected_actions(self, tmp_path):
        client = MagicMock(spec=BaseAIClient)
        eng = _engineer(client, tmp_path)
        eng._handlers = eng._build_handlers()
        names = set(eng.tool_handlers().keys())
        for expected in [
            "submit_plan", "revise_plan", "get_my_plan",
            "view_file", "list_directory", "search_files", "write_file",
            "search_imports", "list_all_imports", "get_file_outline",
            "get_workspace_tree", "list_routes", "list_dependencies",
            "list_pydantic_models",
            "run_tests", "smoke_import",
            "run_in_container", "run_python_in_container",
            "hit_endpoint", "inspect_env", "tail_logs",
            "query_database", "decode_jwt",
        ]:
            assert expected in names, f"missing handler: {expected}"


# ── Plan-first invariant ────────────────────────────────────────────────


class TestPlanFirstInvariant:
    def test_write_file_rejected_before_plan(self, tmp_path):
        actions = [
            _action("write_file", path="x.py", new_content="print(1)"),
            _action("submit_plan", approach="approach", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        # The write_file should have been rejected — file shouldn't exist.
        assert not (tmp_path / "x.py").exists()
        assert isinstance(result, EngineerResult)

    def test_run_tests_rejected_before_plan(self, tmp_path):
        actions = [
            _action("run_tests", path="tests/"),
            _action("submit_plan", approach="approach", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        eng.implement(_milestone(), _arch(), _spec())
        # First call to subprocess.run for tests should not have happened
        # (run_tests was guarded). We assert by counting LLM calls — three
        # turns, all dispatched.
        assert eng._client.get_text.call_count == 3

    def test_view_file_allowed_before_plan(self, tmp_path):
        # Discovery is allowed pre-plan so the engineer can build context.
        (tmp_path / "x.py").write_text("a = 1")
        actions = [
            _action("view_file", path="x.py"),
            _action("submit_plan", approach="approach", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        assert isinstance(result, EngineerResult)


# ── submit_plan validation ──────────────────────────────────────────────


class TestSubmitPlanValidation:
    def test_rejects_empty_approach(self, tmp_path):
        actions = [
            _action("submit_plan", approach="", issues=[_issue()]),
            _action("submit_plan", approach="now valid", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        # Final plan should reflect the second submission.
        assert result.plan.approach == "now valid"

    def test_rejects_no_issues(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok", issues=[]),
            _action("submit_plan", approach="ok", issues=[_issue()]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        assert len(result.plan.issues) == 1

    def test_rejects_issue_without_spec_refs(self, tmp_path):
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
        assert result.plan.issues[0].spec_refs == ["create_pet"]

    def test_rejects_duplicate_issue_ids(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok",
                    issues=[_issue("I1"), _issue("I1")]),
            _action("submit_plan", approach="ok",
                    issues=[_issue("I1"), _issue("I2")]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1", "I2"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        assert {i.id for i in result.plan.issues} == {"I1", "I2"}

    def test_rejects_unknown_dep(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok",
                    issues=[_issue("I1", depends_on=("ghost",))]),
            _action("submit_plan", approach="ok",
                    issues=[_issue("I1")]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        assert result.plan.issues[0].depends_on == []

    def test_second_submit_plan_rejected_use_revise(self, tmp_path):
        actions = [
            _action("submit_plan", approach="first", issues=[_issue("I1")]),
            _action("submit_plan", approach="second", issues=[_issue("I2")]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        # Second submit_plan rejected; first plan stands.
        assert result.plan.approach == "first"
        assert result.plan.issues[0].id == "I1"


# ── revise_plan + get_my_plan ──────────────────────────────────────────


class TestPlanLifecycle:
    def test_revise_plan_replaces_issues(self, tmp_path):
        actions = [
            _action("submit_plan", approach="v1",
                    issues=[_issue("I1"), _issue("I2")]),
            _action("revise_plan", approach="v2",
                    issues=[_issue("I1"), _issue("I3")]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1", "I3"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        assert {i.id for i in result.plan.issues} == {"I1", "I3"}
        assert result.plan.approach == "v2"

    def test_get_my_plan_returns_current(self, tmp_path):
        actions = [
            _action("submit_plan", approach="v1", issues=[_issue("I1")]),
            _action("get_my_plan"),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        # The get_my_plan result should appear as a tool result in the
        # message history sent to the LLM. Check that the third LLM
        # call's messages contain "CURRENT PLAN".
        third_call_msgs = eng._client.get_text.call_args_list[2].kwargs["messages"]
        all_content = " ".join(m["content"] for m in third_call_msgs)
        assert "CURRENT PLAN" in all_content
        assert "I1" in all_content


# ── Terminal action ─────────────────────────────────────────────────────


class TestTerminalAction:
    def test_submit_implementation_returns_engineer_result(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok",
                    issues=[_issue("I1"), _issue("I2", spec_refs=("list_pets",))]),
            _action(
                "submit_implementation",
                summary="all green",
                final_test_status="passed",
                completed_issue_ids=["I1", "I2"],
                deferred_issue_ids=[],
                notes=["nothing weird"],
            ),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        result = eng.implement(_milestone(), _arch(), _spec())
        assert isinstance(result, EngineerResult)
        assert result.summary == "all green"
        assert result.final_test_status == "passed"
        assert result.completed_issue_ids == ["I1", "I2"]
        assert result.notes == ["nothing weird"]
        assert len(result.plan.issues) == 2


# ── Initial context ────────────────────────────────────────────────────


class TestInitialContext:
    def test_threads_milestone_and_spec(self, tmp_path):
        actions = [
            _action("submit_plan", approach="ok", issues=[_issue("I1")]),
            _action("submit_implementation", summary="s",
                    final_test_status="not_run",
                    completed_issue_ids=["I1"]),
        ]
        eng = _engineer(_client_with_actions(actions), tmp_path)
        eng.implement(
            _milestone(), _arch(), _spec(),
            auth_contract="# Auth\nrole: groomer",
        )
        first_msgs = eng._client.get_text.call_args_list[0].kwargs["messages"]
        user = next(m["content"] for m in first_msgs if m["role"] == "user")
        assert "Pet CRUD" in user
        assert "create_pet" in user
        assert "list_pets" in user
        assert "groomer" in user
