"""
Functional test: medium complexity — contact book with models, storage, and search.

Multiple entities, file I/O, and filtering logic.

Run with:
    pytest bizniz/engineer/tests/functional/test_engineer_medium.py -m functional -v
"""
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


PROBLEM_STATEMENT = (
    "Build a Python contact book application. "
    "It should have a Contact data class with fields: name (str), phone (str), "
    "email (str), and group (str, e.g. 'family', 'work', 'friends'). "
    "Implement a ContactBook class that can: "
    "1) Add a contact, "
    "2) Remove a contact by name, "
    "3) Search contacts by name substring (case-insensitive), "
    "4) List all contacts in a given group, "
    "5) Export all contacts to a list of dicts. "
    "Use in-memory storage (a list). No database or file I/O needed."
)


@pytest.mark.functional
def test_contact_book_full_pipeline(api_key, workspace_path):
    """Medium complexity: contact book with model, CRUD, search, and group filtering."""
    config = BiznizConfig(api_key=api_key)
    engineer_client = config.make_engineer_client()
    workspace = LocalWorkspace(root=str(workspace_path))

    status_messages = []

    with Engineer(
        client=engineer_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=lambda suggested_model=None, image_name=None: _make_orchestrator(
            config, workspace, suggested_model=suggested_model, image_name=image_name,
        ),
        on_status_message=lambda msg: status_messages.append(msg),
    ) as engineer:

        analysis = engineer.analyze(PROBLEM_STATEMENT)

        assert analysis.problem_id is not None
        assert analysis.architecture is not None
        assert len(analysis.architecture.namespaces) > 0
        assert len(analysis.issues) >= 2, (
            f"Expected at least 2 issues for contact book, got {len(analysis.issues)}"
        )

        # Dispatch all issues
        results = []
        for issue in analysis.issues:
            result = engineer.dispatch(issue.db_id)
            results.append(result)
            print(f"  Issue #{issue.db_id} '{issue.title}': "
                  f"{'PASS' if result.success else 'FAIL'} ({result.iterations} iters)")

    successes = [r for r in results if r.success]
    total = len(results)
    print(f"\n  Contact book: {len(successes)}/{total} issues resolved")

    # Most issues should succeed
    assert len(successes) >= total // 2, (
        f"Too many failures: {len(successes)}/{total} passed"
    )

    # Verify workspace has source and test files
    files = [str(f) for f in workspace.list_relative_files()]
    py_source = [f for f in files if f.endswith(".py") and not f.startswith("tests/") and not f.startswith(".")]
    py_tests = [f for f in files if f.endswith(".py") and f.startswith("tests/")]
    assert len(py_source) > 0, f"No source files: {files}"
    assert len(py_tests) > 0, f"No test files: {files}"
