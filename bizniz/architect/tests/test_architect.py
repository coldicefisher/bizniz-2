"""Unit tests for the v2 Architect agent (mocked AI client).

The Architect is a single-call agent: ``decompose()`` and ``evolve()``
each make one LLM round-trip (modulo retries). No tools, no
inheritance, no orchestration.
"""
import json
from unittest.mock import MagicMock

import pytest

from bizniz.architect.architect import Architect
from bizniz.architect.types import (
    ArchitectBadAIResponseError,
    ServiceDefinition,
    SystemArchitecture,
)
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.lib.llm_utils import LLMCallError
from bizniz.planner.types import Milestone


# ── Fixtures ─────────────────────────────────────────────────────────────────

VALID_DECOMPOSE = {
    "project_name": "Mini CRM",
    "project_slug": "mini_crm",
    "description": "A small CRM with auth, contacts, and deals.",
    "services": [
        {
            "name": "database",
            "service_type": "database",
            "framework": "postgres",
            "language": "sql",
            "description": "Primary relational store.",
            "workspace_name": "postgres",
            "port": 5432,
            "depends_on": [],
            "requirements": [],
            "skeleton": "none",
        },
        {
            "name": "auth",
            "service_type": "auth",
            "framework": "fusionauth",
            "language": "yaml",
            "description": "Identity provider.",
            "workspace_name": "fusionauth",
            "port": 9011,
            "depends_on": ["database"],
            "requirements": [],
            "skeleton": "none",
        },
        {
            "name": "backend",
            "service_type": "backend",
            "framework": "fastapi",
            "language": "python",
            "description": "REST API for the CRM.",
            "workspace_name": "backend",
            "port": 8000,
            "depends_on": ["database", "auth"],
            "requirements": ["fastapi", "uvicorn", "pydantic"],
            "skeleton": "fastapi",
        },
        {
            "name": "frontend",
            "service_type": "frontend",
            "framework": "react",
            "language": "typescript",
            "description": "React UI.",
            "workspace_name": "frontend",
            "port": 5173,
            "depends_on": ["backend", "auth"],
            "requirements": ["react", "react-dom"],
            "skeleton": "react",
        },
    ],
}


def _ai_response(data):
    text = json.dumps(data)
    return text, "job-id", [{"role": "assistant", "content": text}]


@pytest.fixture
def mock_client():
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = _ai_response(VALID_DECOMPOSE)
    return client


def _existing_arch():
    return SystemArchitecture(
        project_name="Mini CRM",
        project_slug="mini_crm",
        description="seed",
        services=[
            ServiceDefinition(
                name="backend",
                service_type="backend",
                framework="fastapi",
                language="python",
                description="API",
                workspace_name="backend",
                port=8000,
                depends_on=["database", "auth"],
                requirements=["fastapi"],
                skeleton="fastapi",
                image_name="mini_crm-backend:dev",
            ),
        ],
    )


# ── decompose() ──────────────────────────────────────────────────────────────

class TestDecompose:
    def test_returns_typed_architecture(self, mock_client):
        architect = Architect(client=mock_client)
        arch = architect.decompose("Build a CRM", "Mini CRM")
        assert isinstance(arch, SystemArchitecture)
        assert arch.project_name == "Mini CRM"
        assert arch.project_slug == "mini_crm"
        assert len(arch.services) == 4
        assert {s.name for s in arch.services} == {"database", "auth", "backend", "frontend"}

    def test_marks_every_service_as_new(self, mock_client):
        architect = Architect(client=mock_client)
        arch = architect.decompose("Build a CRM", "Mini CRM")
        assert all(s.evolve_state == "new" for s in arch.services)

    def test_normalizes_service_type_lowercase(self):
        """ServiceDefinition validator normalizes mixed-case framework
        names — verify the agent path passes input through cleanly."""
        raw = json.loads(json.dumps(VALID_DECOMPOSE))
        raw["services"][2]["framework"] = "FastAPI"
        raw["services"][2]["language"] = "Python"
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _ai_response(raw)
        architect = Architect(client=client)
        arch = architect.decompose("Build a CRM", "Mini CRM")
        backend = next(s for s in arch.services if s.name == "backend")
        assert backend.framework == "fastapi"
        assert backend.language == "python"

    def test_raises_on_empty_services(self):
        bad = json.loads(json.dumps(VALID_DECOMPOSE))
        bad["services"] = []
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _ai_response(bad)
        architect = Architect(client=client)
        with pytest.raises(ArchitectBadAIResponseError):
            architect.decompose("Build", "X")

    def test_retries_on_empty_response(self):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.side_effect = [
            ("", "job-id", []),
            ("", "job-id", []),
            _ai_response(VALID_DECOMPOSE),
        ]
        architect = Architect(client=client, max_retries=3)
        arch = architect.decompose("Build", "X")
        assert len(arch.services) == 4
        assert client.get_text.call_count == 3

    def test_raises_after_max_retries(self):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = ("", "job-id", [])
        architect = Architect(client=client, max_retries=2)
        with pytest.raises(LLMCallError):
            architect.decompose("Build", "X")


# ── evolve() ────────────────────────────────────────────────────────────────

class TestEvolve:
    def test_preserves_existing_services_with_unchanged_tag(self, mock_client):
        """When the milestone adds NEW services and the AI's response
        contains both the existing and the new, existing keeps identity-
        bearing fields (image_name, skeleton, port) from the prior arch."""
        evolved_payload = {
            "project_name": "Mini CRM",
            "project_slug": "mini_crm",
            "description": "Now with frontend.",
            "services": [
                {
                    "name": "backend",
                    "service_type": "backend",
                    "framework": "fastapi",
                    "language": "python",
                    "description": "API (extended)",
                    "workspace_name": "backend",
                    "port": 8000,
                    "depends_on": ["database", "auth"],
                    "requirements": ["fastapi", "sqlalchemy"],
                    "skeleton": "fastapi",
                    "evolve_state": "extended",
                },
                {
                    "name": "frontend",
                    "service_type": "frontend",
                    "framework": "react",
                    "language": "typescript",
                    "description": "React UI",
                    "workspace_name": "frontend",
                    "port": 5173,
                    "depends_on": ["backend"],
                    "requirements": ["react"],
                    "skeleton": "react",
                    "evolve_state": "new",
                },
            ],
        }
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _ai_response(evolved_payload)
        architect = Architect(client=client)

        milestone = Milestone(
            sequence_index=1,
            name="Frontend",
            problem_slice="Add a React frontend.",
            use_cases=["user can view the CRM in a browser"],
            success_criteria=["page renders without console errors"],
        )
        evolved = architect.evolve(
            milestone=milestone,
            existing_architecture=_existing_arch(),
            problem_statement="Build a CRM",
            project_name="Mini CRM",
        )

        backend = next(s for s in evolved.services if s.name == "backend")
        assert backend.evolve_state == "extended"
        assert backend.image_name == "mini_crm-backend:dev"
        # New requirement merged in with prior:
        assert "fastapi" in backend.requirements
        assert "sqlalchemy" in backend.requirements

        frontend = next(s for s in evolved.services if s.name == "frontend")
        assert frontend.evolve_state == "new"

    def test_restores_dropped_service_as_unchanged(self):
        """If the AI forgets an existing service, the Architect restores
        it as unchanged. This defends against weak models that 'forget'
        services across retries."""
        sparse_payload = {
            "project_name": "Mini CRM",
            "project_slug": "mini_crm",
            "description": "shrunk",
            "services": [
                {
                    "name": "frontend",
                    "service_type": "frontend",
                    "framework": "react",
                    "language": "typescript",
                    "description": "React UI",
                    "workspace_name": "frontend",
                    "port": 5173,
                    "depends_on": ["backend"],
                    "requirements": ["react"],
                    "skeleton": "react",
                    "evolve_state": "new",
                },
            ],
        }
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _ai_response(sparse_payload)
        architect = Architect(client=client)

        milestone = Milestone(
            sequence_index=1, name="Frontend", problem_slice="Add a React frontend.",
        )
        evolved = architect.evolve(
            milestone=milestone,
            existing_architecture=_existing_arch(),
            problem_statement="Build a CRM",
            project_name="Mini CRM",
        )

        names = {s.name for s in evolved.services}
        assert "backend" in names  # restored
        assert "frontend" in names
        backend = next(s for s in evolved.services if s.name == "backend")
        assert backend.evolve_state == "unchanged"

    def test_preserves_image_name_and_workspace_on_extend(self):
        """Identity-bearing fields (image_name, workspace_name, skeleton)
        come from the prior architecture even if the AI omits or alters
        them."""
        payload = {
            "project_name": "Mini CRM",
            "project_slug": "mini_crm",
            "description": "x",
            "services": [
                {
                    "name": "backend",
                    "service_type": "backend",
                    "framework": "fastapi",
                    "language": "python",
                    "description": "extended",
                    "workspace_name": "wrong_name_from_ai",
                    "port": 9999,
                    "depends_on": ["database"],
                    "requirements": [],
                    "skeleton": "wrong_skeleton",
                    "evolve_state": "extended",
                },
            ],
        }
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _ai_response(payload)
        architect = Architect(client=client)

        milestone = Milestone(
            sequence_index=1, name="Extend", problem_slice="Add stuff.",
        )
        evolved = architect.evolve(
            milestone=milestone,
            existing_architecture=_existing_arch(),
            problem_statement="Build a CRM",
            project_name="Mini CRM",
        )
        backend = next(s for s in evolved.services if s.name == "backend")
        assert backend.workspace_name == "backend"  # from prior, not AI
        assert backend.port == 8000
        assert backend.skeleton == "fastapi"
        assert backend.image_name == "mini_crm-backend:dev"
