"""
Example: Auto Architect

Decomposes a problem into a service-based architecture,
creates workspaces, generates Dockerfiles and docker-compose.yml,
and dispatches AutoEngineer instances for application services.

Requirements:
    - OPENAI_API_KEY environment variable set (or .env file)
"""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from bizniz.autocoder.autocoder import Autocoder
from bizniz.autodebugger.autodebugger import Autodebugger
from bizniz.agentic_debugger.agentic_debugger import AgenticDebugger
from bizniz.autotester.autotester import Autotester
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.environment.pytest_environment import PytestEnvironment
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.engineer.auto_engineer import AutoEngineer
from bizniz.architect.auto_architect import AutoArchitect
from bizniz.workspace.local_workspace import LocalWorkspace


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


def _make_orchestrator(config, workspace, suggested_model=None):
    sandbox = DockerExecutionEnvironment()
    pytest_env = PytestEnvironment(workspace_root=workspace.root)

    def debugger_factory():
        fresh_client = config.make_client()
        return AgenticDebugger(
            client=fresh_client, workspace=workspace, environment=pytest_env,
        )

    def client_factory(model_name):
        return config.make_client(model=model_name)

    issue_client = config.make_client(model=suggested_model) if suggested_model else config.make_client()

    return CodingOrchestrator(
        autocoder=Autocoder(client=issue_client, environment=sandbox, workspace=workspace),
        autotester=Autotester(client=issue_client, environment=sandbox, workspace=workspace),
        autodebugger=Autodebugger(client=issue_client, environment=sandbox, workspace=workspace),
        test_environment=pytest_env,
        workspace=workspace,
        client=issue_client,
        client_factory=client_factory,
        debugger_factory=debugger_factory,
        model_progression=config.make_model_progression(),
        max_iterations=config.max_iterations,
    )


def _make_engineer(config, workspace):
    def orchestrator_factory(suggested_model=None):
        return _make_orchestrator(config, workspace, suggested_model=suggested_model)

    engineer_client = config.make_engineer_client()

    return AutoEngineer(
        client=engineer_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=orchestrator_factory,
    )


if __name__ == "__main__":

    config = BiznizConfig.find_and_load()
    architect_client = config.make_client(model="gpt-4o")

    project_name = "Pet Groomer"
    workspace_parent = Path.home() / "bizniz_projects"
    workspace_parent.mkdir(parents=True, exist_ok=True)

    root_workspace = LocalWorkspace.from_name(project_name, parent=workspace_parent)

    architect = AutoArchitect(
        client=architect_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=root_workspace,
        engineer_factory=lambda ws: _make_engineer(config, ws),
        workspace_parent=str(workspace_parent),
        on_status_message=lambda msg: print(f"  {msg}"),
    )

    print(f"\n{'='*60}")
    print(f"  Auto Architect: {project_name}")
    print(f"{'='*60}\n")

    result = architect.build(PROBLEM_STATEMENT, project_name)

    print(f"\n{'='*60}")
    print(f"  Results")
    print(f"{'='*60}")
    print(f"  Project: {result.project_name}")
    print(f"  Services: {len(result.architecture.services)}")
    print(f"  Docker compose: {result.docker_compose_path}")
    print()
    for sr in result.service_results:
        status_str = "PASS" if sr.success else "FAIL"
        print(f"  {sr.service_name}: {status_str} ({sr.issues_passed}/{sr.issues_total} issues)")
    print()
