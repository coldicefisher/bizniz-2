"""Tests for ``ServicePlannerWithScaffold`` — focuses on v4-relevant
properties: ``depends_on`` round-trips, ``seeded_files`` returned,
issue invariants stamped.

The production ``ServicePlanner`` already has extensive tests in
``test_service_planner.py``; this file covers the v3/v4 scaffold
variant specifically.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.service_planner.agent import ServicePlannerError
from bizniz.service_planner.scaffolded import (
    ScaffoldedPlanResult,
    ServicePlannerWithScaffold,
)


def _service() -> ServiceDefinition:
    return ServiceDefinition(
        name="backend",
        service_type="backend",
        framework="fastapi",
        language="python",
        workspace_name="backend",
        port=8000,
        description="API backend",
        depends_on=[],
    )


def _arch() -> SystemArchitecture:
    return SystemArchitecture(
        project_name="test",
        project_slug="test",
        description="test",
        services=[_service()],
    )


def _enriched_spec():
    """Minimal EnrichedSpec; the prompt builder needs the .capabilities
    + .milestone_name attrs."""
    from bizniz.quality_engineer.types import EnrichedSpec
    return EnrichedSpec(milestone_name="M1", capabilities=[])


def _ok_llm_output_with_deps() -> dict:
    """Mock LLM output for the scaffolded planner — covers the
    multi-issue depends_on case."""
    return {
        "issues": [
            {
                "id": "BE-001",
                "title": "User model",
                "description": "Add User Pydantic model.",
                "target_files": ["app/models/user.py"],
                "test_files": ["tests/test_user_model.py"],
                "success_criteria": ["User schema defined"],
                "spec_refs": ["user_schema"],
                "depends_on": [],
            },
            {
                "id": "BE-002",
                "title": "User repository",
                "description": "CRUD against User model.",
                "target_files": ["app/repositories/user.py"],
                "test_files": ["tests/test_user_repo.py"],
                "success_criteria": ["User CRUD covered"],
                "spec_refs": ["user_persistence"],
                "depends_on": ["BE-001"],
            },
            {
                "id": "BE-003",
                "title": "/users route",
                "description": "Expose User CRUD as HTTP routes.",
                "target_files": ["app/api/routes/users.py"],
                "test_files": ["tests/test_users_routes.py"],
                "success_criteria": ["/users covered by integration tests"],
                "spec_refs": ["user_endpoint"],
                "depends_on": ["BE-002"],
            },
        ],
        "seeded_files": [
            {
                "path": "app/models/user.py",
                "content": "from pydantic import BaseModel\nclass User(BaseModel):\n    raise NotImplementedError('BE-001')\n",
                "rationale": "User schema; filled by BE-001.",
            },
            {
                "path": "app/repositories/user.py",
                "content": "from app.models.user import User\ndef create_user(u: User) -> User:\n    raise NotImplementedError('BE-002')\n",
                "rationale": "User CRUD; filled by BE-002.",
            },
            {
                "path": "app/api/routes/users.py",
                "content": "from fastapi import APIRouter\nrouter = APIRouter()\n",
                "rationale": "User routes; filled by BE-003.",
            },
        ],
    }


class TestDependsOnRoundTrip:
    def test_planner_emits_depends_on_through_to_issue_model(self):
        with patch(
            "bizniz.service_planner.scaffolded.call_with_retry",
            return_value=_ok_llm_output_with_deps(),
        ):
            planner = ServicePlannerWithScaffold(
                client=MagicMock(spec=BaseAIClient),
            )
            result = planner.plan_service(
                architecture=_arch(),
                enriched_spec=_enriched_spec(),
                service=_service(),
            )
        assert isinstance(result, ScaffoldedPlanResult)
        by_id = {i.id: i for i in result.issues}
        assert by_id["BE-001"].depends_on == []
        assert by_id["BE-002"].depends_on == ["BE-001"]
        assert by_id["BE-003"].depends_on == ["BE-002"]

    def test_issue_invariants_stamped(self):
        """service + language are stamped from the ServiceDefinition,
        regardless of what the LLM emitted."""
        out = _ok_llm_output_with_deps()
        with patch(
            "bizniz.service_planner.scaffolded.call_with_retry",
            return_value=out,
        ):
            planner = ServicePlannerWithScaffold(
                client=MagicMock(spec=BaseAIClient),
            )
            result = planner.plan_service(
                architecture=_arch(),
                enriched_spec=_enriched_spec(),
                service=_service(),
            )
        for i in result.issues:
            assert i.service == "backend"
            assert i.language == "python"


class TestSeededFiles:
    def test_seeded_files_returned(self):
        with patch(
            "bizniz.service_planner.scaffolded.call_with_retry",
            return_value=_ok_llm_output_with_deps(),
        ):
            planner = ServicePlannerWithScaffold(
                client=MagicMock(spec=BaseAIClient),
            )
            result = planner.plan_service(
                architecture=_arch(),
                enriched_spec=_enriched_spec(),
                service=_service(),
            )
        paths = {s.path for s in result.seeded_files}
        assert paths == {
            "app/models/user.py",
            "app/repositories/user.py",
            "app/api/routes/users.py",
        }

    def test_zero_seeded_files_raises(self):
        bad = {"issues": _ok_llm_output_with_deps()["issues"], "seeded_files": []}
        with patch(
            "bizniz.service_planner.scaffolded.call_with_retry",
            return_value=bad,
        ):
            planner = ServicePlannerWithScaffold(
                client=MagicMock(spec=BaseAIClient),
            )
            with pytest.raises(ServicePlannerError, match="0 seeded_files"):
                planner.plan_service(
                    architecture=_arch(),
                    enriched_spec=_enriched_spec(),
                    service=_service(),
                )

    def test_zero_issues_raises(self):
        bad = {"issues": [], "seeded_files": _ok_llm_output_with_deps()["seeded_files"]}
        with patch(
            "bizniz.service_planner.scaffolded.call_with_retry",
            return_value=bad,
        ):
            planner = ServicePlannerWithScaffold(
                client=MagicMock(spec=BaseAIClient),
            )
            with pytest.raises(ServicePlannerError, match="0 issues"):
                planner.plan_service(
                    architecture=_arch(),
                    enriched_spec=_enriched_spec(),
                    service=_service(),
                )


class TestPromptIncludesParallelGuidance:
    def test_system_prompt_mentions_parallel_runner(self):
        """The v4-added prompt nudge about depends_on being load-bearing
        for the parallel runner is present."""
        from bizniz.service_planner.prompts.system_prompt_with_scaffold import (
            SERVICE_PLANNER_SCAFFOLD_SYSTEM_PROMPT,
        )
        assert "PARALLEL EXECUTION" in SERVICE_PLANNER_SCAFFOLD_SYSTEM_PROMPT
        assert "depends_on" in SERVICE_PLANNER_SCAFFOLD_SYSTEM_PROMPT
        assert "file-overlap" in SERVICE_PLANNER_SCAFFOLD_SYSTEM_PROMPT
