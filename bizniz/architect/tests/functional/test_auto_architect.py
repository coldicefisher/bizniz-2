"""
Functional test: Auto Architect — website decomposition and engineering.

Tests that the architect can decompose a problem into services,
create project structure, build Docker images, and dispatch engineers.

Run with:
    pytest bizniz/architect/tests/functional/test_architect.py -m functional -v
"""
import os
import pytest

from bizniz.agents.coder.coder import Coder
from bizniz.agents.debugger.quick import QuickDebugger
from bizniz.agents.debugger.agentic import AgenticDebugger
from bizniz.tester.tester import Tester
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.engineer.engineer import Engineer
from bizniz.architect.architect import Architect
from bizniz.workspace.local_workspace import LocalWorkspace


def _make_orchestrator(config, workspace, suggested_model=None, image_name=None):
    sandbox = DockerExecutionEnvironment()
    test_env = DockerPytestEnvironment(
        workspace_root=workspace.root,
        image=image_name or "bizniz-python-runner",
    )

    def debugger_factory():
        fresh_client = config.make_client()
        return AgenticDebugger(
            client=fresh_client, workspace=workspace, environment=test_env,
        )

    def client_factory(model_name):
        return config.make_client(model=model_name)

    issue_client = config.make_client(model=suggested_model) if suggested_model else config.make_client()

    return CodingOrchestrator(
        coder=Coder(client=issue_client, environment=sandbox, workspace=workspace),
        tester=Tester(client=issue_client, environment=sandbox, workspace=workspace),
        quick_debugger=QuickDebugger(client=issue_client, environment=sandbox, workspace=workspace),
        test_environment=test_env,
        workspace=workspace,
        client=issue_client,
        client_factory=client_factory,
        debugger_factory=debugger_factory,
        model_progression=config.make_model_progression(),
        max_iterations=config.max_iterations,
    )


def _make_engineer(config, workspace, on_status_message=None, image_name=None):
    """Create an Engineer context manager for a service workspace."""

    def orchestrator_factory(suggested_model=None):
        return _make_orchestrator(
            config, workspace, suggested_model=suggested_model, image_name=image_name,
        )

    engineer_client = config.make_engineer_client()

    return Engineer(
        client=engineer_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=orchestrator_factory,
        on_status_message=on_status_message,
    )


PROBLEM_STATEMENT = (
    "Build a web application for a pet grooming salon. "
    "The website should allow customers to: "
    "1) View available grooming services (bath, haircut, nail trim, etc.) with prices, "
    "2) Book an appointment by selecting a service, date, and time slot, "
    "3) View and cancel their existing appointments. "
    "\n\n"
    "The backend should be a REST API with endpoints for services, appointments, "
    "and basic validation (no double-booking the same time slot). "
    "Use in-memory storage for now (no database required)."
)


@pytest.mark.functional
def test_architect_decompose(api_key, workspace_path):
    """Test that the architect can decompose a problem into services."""
    config = BiznizConfig(api_key=api_key)
    architect_client = config.make_client(model="gpt-4o")
    workspace = LocalWorkspace(root=str(workspace_path))

    status_messages = []

    architect = Architect(
        client=architect_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        engineer_factory=lambda ws, on_status_message=None, image_name=None: _make_engineer(
            config, ws, on_status_message=on_status_message, image_name=image_name,
        ),
        project_parent=str(workspace_path),
        on_status_message=lambda msg: status_messages.append(msg),
    )

    architecture = architect.decompose(PROBLEM_STATEMENT, "Pet Groomer")

    # Should produce a valid architecture
    assert architecture.project_name is not None
    assert architecture.project_slug is not None
    assert len(architecture.services) >= 2, (
        f"Expected at least 2 services (backend + frontend), "
        f"got {len(architecture.services)}: "
        f"{[s.name for s in architecture.services]}"
    )
    assert architecture.docker_compose, "docker-compose.yml should not be empty"

    # Should have at least a backend service
    service_types = [s.service_type for s in architecture.services]
    assert "backend" in service_types, f"Expected a backend service, got types: {service_types}"

    # Each service should have required fields
    for svc in architecture.services:
        assert svc.name, "Service must have a name"
        assert svc.framework, "Service must have a framework"
        assert svc.language, "Service must have a language"
        assert svc.workspace_name, "Service must have a workspace_name"

    print(f"\n  Architecture: {architecture.project_name}")
    print(f"  Services: {', '.join(s.name for s in architecture.services)}")
    print(f"  Docker compose length: {len(architecture.docker_compose)} chars")


@pytest.mark.functional
def test_architect_build_backend_only(api_key, workspace_path):
    """Test full pipeline: decompose + build project structure + dispatch engineer."""
    config = BiznizConfig(api_key=api_key)
    architect_client = config.make_client(model="gpt-4o")
    workspace = LocalWorkspace(root=str(workspace_path))

    status_messages = []

    # Simple problem that only needs a backend
    simple_problem = (
        "Build a Python REST API for managing a todo list. "
        "Endpoints: GET /todos (list all), POST /todos (create), "
        "DELETE /todos/{id} (delete). Use in-memory storage. "
        "Each todo has: id (int), title (str), completed (bool). "
        "Use FastAPI."
    )

    architect = Architect(
        client=architect_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        engineer_factory=lambda ws, on_status_message=None, image_name=None: _make_engineer(
            config, ws, on_status_message=on_status_message, image_name=image_name,
        ),
        project_parent=str(workspace_path),
        on_status_message=lambda msg: status_messages.append(msg),
    )

    result = architect.build(simple_problem, "Todo App")

    assert result.project_name == "Todo App"
    assert result.architecture is not None
    assert len(result.architecture.services) >= 1
    assert result.docker_compose_path is not None
    assert result.project_root is not None

    # Check project structure
    from pathlib import Path
    project_root = Path(result.project_root)
    dev_root = project_root / "infra" / "development"
    assert dev_root.exists(), f"Development directory should exist at {dev_root}"
    assert (dev_root / "docker-compose.yml").exists()
    assert (dev_root / ".env").exists()

    # At least one service should have been dispatched
    assert len(result.service_results) >= 1

    print(f"\n  Build result: {result.project_name}")
    print(f"  Project root: {result.project_root}")
    print(f"  Services dispatched: {len(result.service_results)}")
    for sr in result.service_results:
        status = "PASS" if sr.success else "FAIL"
        print(f"    {sr.service_name}: {status} ({sr.issues_passed}/{sr.issues_total})")
