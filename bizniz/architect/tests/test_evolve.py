"""Unit tests for AutoArchitect.evolve() (mocked AI client)."""
import json
from unittest.mock import MagicMock

import pytest

from bizniz.architect.auto_architect import AutoArchitect
from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.planner.types import Milestone
from bizniz.workspace.local_workspace import LocalWorkspace


def _ai_response(data):
    text = json.dumps(data)
    return text, "job-id", [{"role": "assistant", "content": text}]


def _service(**kw):
    base = dict(
        name="x", service_type="backend", framework="fastapi", language="python",
        description="x", workspace_name="x", port=8000, depends_on=[], requirements=[],
        skeleton="fastapi",
    )
    base.update(kw)
    return ServiceDefinition(**base)


@pytest.fixture
def workspace(tmp_path):
    return LocalWorkspace(root=tmp_path / "ws")


@pytest.fixture
def architect(workspace):
    """Build an AutoArchitect with a default-empty mock client. Tests
    set client.get_text.return_value to whatever they need."""
    client = MagicMock(spec=BaseAIClient)
    env = MagicMock(spec=BaseExecutionEnvironment)
    arch = AutoArchitect(
        client=client,
        environment=env,
        workspace=workspace,
        engineer_factory=lambda *a, **kw: None,
    )
    arch._client = client  # ensure access for tests
    return arch


@pytest.fixture
def existing_arch():
    return SystemArchitecture(
        project_name="Mini CRM",
        project_slug="mini_crm",
        description="x",
        services=[
            _service(
                name="auth", service_type="auth", framework="fusionauth",
                language="yaml", workspace_name="fusionauth", port=9011,
                skeleton="none", evolve_state="unchanged",
            ),
            _service(
                name="postgres", service_type="database", framework="postgres",
                language="sql", workspace_name="postgres", port=5432,
                skeleton="none", evolve_state="unchanged",
            ),
            _service(
                name="backend", service_type="backend", framework="fastapi",
                language="python", workspace_name="backend", port=8001,
                requirements=["fastapi"],
                depends_on=["postgres", "auth"],
                skeleton="fastapi", evolve_state="unchanged",
            ),
        ],
    )


@pytest.fixture
def deal_milestone():
    return Milestone(
        sequence_index=1,
        name="Deals attached to contacts",
        problem_slice="Add deals (name, value, stage) attached to contacts.",
        use_cases=["user can attach a deal to a contact"],
        success_criteria=["deal totals roll up correctly"],
        depends_on_names=["Contact CRUD"],
        estimated_effort="M",
    )


# ── New service added ────────────────────────────────────────────────────────

def test_evolve_adds_new_service(architect, existing_arch, deal_milestone):
    """AI returns existing services unchanged + a new 'reporter' service."""
    architect._client.get_text.return_value = _ai_response({
        "project_name": "Mini CRM",
        "project_slug": "mini_crm",
        "description": "Adds a reporter service",
        "services": [
            {
                "name": "auth", "service_type": "auth", "framework": "fusionauth",
                "language": "yaml", "description": "auth", "workspace_name": "fusionauth",
                "port": 9011, "depends_on": [], "requirements": [],
                "skeleton": "none", "evolve_state": "unchanged",
            },
            {
                "name": "postgres", "service_type": "database", "framework": "postgres",
                "language": "sql", "description": "db", "workspace_name": "postgres",
                "port": 5432, "depends_on": [], "requirements": [],
                "skeleton": "none", "evolve_state": "unchanged",
            },
            {
                "name": "backend", "service_type": "backend", "framework": "fastapi",
                "language": "python", "description": "api", "workspace_name": "backend",
                "port": 8001, "depends_on": ["postgres", "auth"],
                "requirements": ["fastapi", "pydantic"],
                "skeleton": "fastapi", "evolve_state": "extended",
            },
            {
                "name": "reporter", "service_type": "worker", "framework": "celery",
                "language": "python", "description": "report worker",
                "workspace_name": "reporter", "port": None,
                "depends_on": ["postgres"], "requirements": ["celery"],
                "skeleton": "none", "evolve_state": "new",
            },
        ],
    })

    evolved = architect.evolve(
        milestone=deal_milestone,
        existing_architecture=existing_arch,
        problem_statement="Build a CRM",
        project_name="Mini CRM",
    )

    by_name = {s.name: s for s in evolved.services}
    assert by_name["reporter"].evolve_state == "new"
    assert by_name["backend"].evolve_state == "extended"
    assert by_name["auth"].evolve_state == "unchanged"
    assert by_name["postgres"].evolve_state == "unchanged"


def test_evolve_preserves_existing_service_identity(architect, existing_arch, deal_milestone):
    """Existing services keep their original framework/language/port even
    if the AI tries to change them. We accept new requirements/depends_on
    via merge."""
    architect._client.get_text.return_value = _ai_response({
        "project_name": "Mini CRM",
        "project_slug": "mini_crm",
        "description": "x",
        "services": [
            {
                "name": "auth", "service_type": "auth", "framework": "fusionauth",
                "language": "yaml", "description": "auth", "workspace_name": "fusionauth",
                "port": 9011, "depends_on": [], "requirements": [],
                "skeleton": "none", "evolve_state": "unchanged",
            },
            {
                "name": "postgres", "service_type": "database", "framework": "postgres",
                "language": "sql", "description": "db", "workspace_name": "postgres",
                "port": 5432, "depends_on": [], "requirements": [],
                "skeleton": "none", "evolve_state": "unchanged",
            },
            # AI tries to change backend port + framework. Should be ignored.
            {
                "name": "backend", "service_type": "backend", "framework": "flask",  # AI tried to change
                "language": "python", "description": "api",
                "workspace_name": "backend", "port": 9999,                          # AI tried to change
                "depends_on": ["postgres", "auth", "reporter"],                     # extension OK
                "requirements": ["fastapi", "pydantic", "newpkg"],                   # merge new
                "skeleton": "react",                                                 # AI tried to change
                "evolve_state": "extended",
            },
        ],
    })
    evolved = architect.evolve(
        milestone=deal_milestone,
        existing_architecture=existing_arch,
        problem_statement="x",
        project_name="Mini CRM",
    )
    backend = next(s for s in evolved.services if s.name == "backend")
    # Identity fields preserved
    assert backend.framework == "fastapi"
    assert backend.port == 8001
    assert backend.skeleton == "fastapi"
    # New depends_on and requirements merged
    assert "reporter" in backend.depends_on
    assert "newpkg" in backend.requirements
    assert "fastapi" in backend.requirements  # original retained


def test_evolve_restores_dropped_existing_service(architect, existing_arch, deal_milestone):
    """If the AI drops an existing service, the architect re-adds it as
    unchanged (defensive — services from prior milestones should never
    disappear)."""
    architect._client.get_text.return_value = _ai_response({
        "project_name": "Mini CRM",
        "project_slug": "mini_crm",
        "description": "x",
        "services": [
            {
                "name": "auth", "service_type": "auth", "framework": "fusionauth",
                "language": "yaml", "description": "auth", "workspace_name": "fusionauth",
                "port": 9011, "depends_on": [], "requirements": [],
                "skeleton": "none", "evolve_state": "unchanged",
            },
            # postgres dropped
            {
                "name": "backend", "service_type": "backend", "framework": "fastapi",
                "language": "python", "description": "api", "workspace_name": "backend",
                "port": 8001, "depends_on": [], "requirements": [],
                "skeleton": "fastapi", "evolve_state": "unchanged",
            },
        ],
    })
    evolved = architect.evolve(
        milestone=deal_milestone,
        existing_architecture=existing_arch,
        problem_statement="x",
        project_name="Mini CRM",
    )
    by_name = {s.name: s for s in evolved.services}
    assert "postgres" in by_name
    assert by_name["postgres"].evolve_state == "unchanged"


def test_evolve_empty_existing_makes_everything_new(architect, deal_milestone):
    """Calling evolve() on an empty existing architecture (e.g. milestone 0)
    is essentially a fresh decompose — everything should be 'new'."""
    architect._client.get_text.return_value = _ai_response({
        "project_name": "Mini CRM",
        "project_slug": "mini_crm",
        "description": "fresh start",
        "services": [
            {
                "name": "backend", "service_type": "backend", "framework": "fastapi",
                "language": "python", "description": "api", "workspace_name": "backend",
                "port": 8000, "depends_on": [], "requirements": [],
                "skeleton": "fastapi", "evolve_state": "new",
            },
        ],
    })
    empty = SystemArchitecture(
        project_name="Mini CRM", project_slug="mini_crm", description="", services=[],
    )
    evolved = architect.evolve(
        milestone=deal_milestone,
        existing_architecture=empty,
        problem_statement="x",
        project_name="Mini CRM",
    )
    assert all(s.evolve_state == "new" for s in evolved.services)


# ── Decompose backward-compat ────────────────────────────────────────────────

def test_decompose_tags_all_services_new(architect):
    """Fresh decompose() should tag every service as 'new' — ensures
    the evolve_state field is populated even for non-evolve runs."""
    architect._client.get_text.return_value = _ai_response({
        "project_name": "X", "project_slug": "x", "description": "x",
        "services": [
            {
                "name": "backend", "service_type": "backend", "framework": "fastapi",
                "language": "python", "description": "x", "workspace_name": "backend",
                "port": 8000, "depends_on": [], "requirements": [],
                "skeleton": "fastapi",
            },
        ],
    })
    arch = architect.decompose("Build something", "X")
    assert all(s.evolve_state == "new" for s in arch.services)
