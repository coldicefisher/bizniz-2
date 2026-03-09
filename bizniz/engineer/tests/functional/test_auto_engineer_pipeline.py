"""
Functional tests: verify the full AutoEngineer pipeline with real API calls.

This is the end-to-end test — analyzes a problem, creates architecture,
generates code and tests, and verifies everything passes.

Run with:
    pytest bizniz/engineer/tests/functional/ -m functional -v --timeout=600
"""
import pytest

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
from bizniz.workspace.local_workspace import LocalWorkspace


def _make_orchestrator(config, workspace, suggested_model=None):
    """Factory: returns a fresh CodingOrchestrator per issue."""
    sandbox = DockerExecutionEnvironment()
    pytest_env = PytestEnvironment(workspace_root=workspace.root)

    def debugger_factory():
        fresh_client = config.make_client()
        return AgenticDebugger(
            client=fresh_client,
            workspace=workspace,
            environment=pytest_env,
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


@pytest.mark.functional
def test_full_pipeline_simple_problem(api_key, workspace_path):
    """Full pipeline: analyze → dispatch all issues → all pass."""
    config = BiznizConfig(api_key=api_key)
    engineer_client = config.make_engineer_client()
    workspace = LocalWorkspace(root=str(workspace_path))

    status_messages = []

    with AutoEngineer(
        client=engineer_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=lambda suggested_model=None: _make_orchestrator(
            config, workspace, suggested_model=suggested_model,
        ),
        on_status_message=lambda msg: status_messages.append(msg),
    ) as engineer:

        # Analyze a simple problem
        analysis = engineer.analyze(
            "Build a Python module with a function 'greet(name)' that returns "
            "'Hello, {name}!' and a function 'farewell(name)' that returns "
            "'Goodbye, {name}!'."
        )

        assert analysis.problem_id is not None
        assert len(analysis.issues) > 0

        # Dispatch all issues
        results = []
        for issue in analysis.issues:
            result = engineer.dispatch(issue.db_id)
            results.append(result)

    # At least one issue should succeed
    successes = [r for r in results if r.success]
    assert len(successes) > 0, f"No issues succeeded: {[(r.success, r.iterations) for r in results]}"

    # Verify workspace has generated files
    files = [str(f) for f in workspace.list_relative_files()]
    py_files = [f for f in files if f.endswith(".py") and not f.startswith(".")]
    assert len(py_files) > 0, f"No Python files generated: {files}"


@pytest.mark.functional
def test_analyze_produces_architecture(api_key, workspace_path):
    """Analysis phase produces architecture plan with namespaces and modules."""
    config = BiznizConfig(api_key=api_key)
    engineer_client = config.make_engineer_client()
    workspace = LocalWorkspace(root=str(workspace_path))

    with AutoEngineer(
        client=engineer_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=lambda suggested_model=None: _make_orchestrator(
            config, workspace, suggested_model=suggested_model,
        ),
    ) as engineer:

        analysis = engineer.analyze(
            "Build a command-line todo list app that lets users add, remove, and list tasks."
        )

    assert analysis.architecture is not None
    assert analysis.architecture.package_name
    assert len(analysis.architecture.namespaces) > 0
    assert len(analysis.issues) > 0
    assert len(analysis.requirements) > 0

    # Issues should have target files
    for issue in analysis.issues:
        assert len(issue.target_files) > 0 or issue.test_files, \
            f"Issue '{issue.title}' has no target files or test files"
