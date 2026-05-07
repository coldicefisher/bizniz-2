"""ServicePlanner tests — single-call agent that emits Issue lists."""
import json
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.coder.types import Issue
from bizniz.quality_engineer.types import (
    CapabilitySpec, EnrichedSpec, Field as SpecField,
)
from bizniz.service_planner.agent import ServicePlanner, ServicePlannerError


# ── Fixtures ───────────────────────────────────────────────────────────


def _service():
    return ServiceDefinition(
        name="backend", service_type="backend", framework="fastapi",
        language="python", description="API",
        workspace_name="backend", port=8000, depends_on=[],
        skeleton="fastapi",
    )


def _arch():
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=[_service()],
    )


def _spec():
    return EnrichedSpec(
        milestone_name="M1",
        capabilities=[
            CapabilitySpec(
                id="create_pet", name="Create pet",
                description="Create a pet record",
                inputs=[SpecField(name="name", type="string", required=True)],
                outputs=[], validation_rules=[], error_cases=[],
                edge_cases=[], auth_required=True, allowed_roles=["user"],
                test_scenarios=["happy"],
            ),
        ],
    )


_SENTINEL = object()


def _issue_dict(id_, deps=None, files=_SENTINEL, tests=_SENTINEL, refs=None):
    if files is _SENTINEL:
        files = [f"app/{id_.lower()}.py"]
    if tests is _SENTINEL:
        tests = [f"tests/test_{id_.lower()}.py"]
    return {
        "id": id_,
        "title": f"Implement {id_}",
        "description": f"desc for {id_}",
        "target_files": files,
        "test_files": tests,
        "success_criteria": ["compiles"],
        "spec_refs": refs or ["create_pet"],
        "depends_on": deps or [],
    }


def _client_returning(payload: dict) -> BaseAIClient:
    c = MagicMock(spec=BaseAIClient)
    c.get_text.return_value = (json.dumps(payload), "j", [])
    return c


# ── Happy path ─────────────────────────────────────────────────────────


class TestHappyPath:
    def test_minimal_one_issue(self):
        client = _client_returning({"issues": [_issue_dict("BE-001")]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_service(
            architecture=_arch(),
            enriched_spec=_spec(),
            service=_service(),
        )
        assert len(issues) == 1
        assert isinstance(issues[0], Issue)
        assert issues[0].id == "BE-001"
        assert issues[0].service == "backend"
        assert issues[0].language == "python"

    def test_topo_order_applied(self):
        # Input declares C, A, B with B → A and C → B. Output: A, B, C.
        client = _client_returning({"issues": [
            _issue_dict("C", deps=["B"]),
            _issue_dict("A"),
            _issue_dict("B", deps=["A"]),
        ]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
        )
        assert [i.id for i in issues] == ["A", "B", "C"]

    def test_service_and_language_stamped(self):
        # Even if the LLM omits service/language, we stamp them.
        client = _client_returning({"issues": [
            _issue_dict("BE-001"),
        ]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
        )
        assert issues[0].service == "backend"
        assert issues[0].language == "python"


# ── Validation ─────────────────────────────────────────────────────────


class TestValidation:
    def test_empty_issues_raises(self):
        client = _client_returning({"issues": []})
        planner = ServicePlanner(client=client)
        with pytest.raises(ServicePlannerError, match="0 issues"):
            planner.plan_service(
                architecture=_arch(), enriched_spec=_spec(), service=_service(),
            )

    def test_duplicate_id_raises(self):
        client = _client_returning({"issues": [
            _issue_dict("BE-001"),
            _issue_dict("BE-001"),
        ]})
        planner = ServicePlanner(client=client)
        with pytest.raises(ServicePlannerError, match="duplicate"):
            planner.plan_service(
                architecture=_arch(), enriched_spec=_spec(), service=_service(),
            )

    def test_unknown_dep_raises(self):
        client = _client_returning({"issues": [
            _issue_dict("BE-001", deps=["BE-999"]),
        ]})
        planner = ServicePlanner(client=client)
        with pytest.raises(ServicePlannerError, match="unknown issue id"):
            planner.plan_service(
                architecture=_arch(), enriched_spec=_spec(), service=_service(),
            )

    def test_missing_target_files_raises(self):
        d = _issue_dict("BE-001", files=[])
        client = _client_returning({"issues": [d]})
        planner = ServicePlanner(client=client)
        with pytest.raises(ServicePlannerError, match="target/test file"):
            planner.plan_service(
                architecture=_arch(), enriched_spec=_spec(), service=_service(),
            )

    def test_missing_test_files_raises(self):
        d = _issue_dict("BE-001", tests=[])
        client = _client_returning({"issues": [d]})
        planner = ServicePlanner(client=client)
        with pytest.raises(ServicePlannerError, match="target/test file"):
            planner.plan_service(
                architecture=_arch(), enriched_spec=_spec(), service=_service(),
            )

    def test_cyclic_deps_raises(self):
        client = _client_returning({"issues": [
            _issue_dict("A", deps=["B"]),
            _issue_dict("B", deps=["A"]),
        ]})
        planner = ServicePlanner(client=client)
        with pytest.raises(ServicePlannerError, match="cycle"):
            planner.plan_service(
                architecture=_arch(), enriched_spec=_spec(), service=_service(),
            )


# ── Prompt content ─────────────────────────────────────────────────────


class TestPromptContent:
    def test_prompt_includes_service_and_capability(self):
        client = _client_returning({"issues": [_issue_dict("BE-001")]})
        planner = ServicePlanner(client=client)
        planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user_msg = next(m.content for m in sent if m.role == "user")
        assert "backend" in user_msg.lower()
        assert "create_pet" in user_msg
        assert "Create pet" in user_msg
        assert "name" in user_msg

    def test_prompt_includes_skeleton_when_provided(self):
        client = _client_returning({"issues": [_issue_dict("BE-001")]})
        planner = ServicePlanner(client=client)
        planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
            skeleton_md="## Extension points\n- app/api/routes/",
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user_msg = next(m.content for m in sent if m.role == "user")
        assert "skeleton" in user_msg.lower()
        assert "app/api/routes/" in user_msg

    def test_prompt_includes_auth_when_provided(self):
        client = _client_returning({"issues": [_issue_dict("BE-001")]})
        planner = ServicePlanner(client=client)
        planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
            auth_contract="JWT with rs256...",
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user_msg = next(m.content for m in sent if m.role == "user")
        assert "JWT" in user_msg or "auth" in user_msg.lower()
