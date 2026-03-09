"""
Example: CodingOrchestrator

Iteratively generates code + tests, runs pytest, and repairs until tests pass.
Uses TDD by default (tests first, then code).
Features model escalation on stalls.

Requirements:
    - OPENAI_API_KEY environment variable set (or .env file)
"""
from dotenv import load_dotenv

load_dotenv()

from bizniz.autocoder.autocoder import Autocoder
from bizniz.autodebugger.autodebugger import Autodebugger
from bizniz.agentic_debugger.agentic_debugger import AgenticDebugger
from bizniz.autotester.autotester import Autotester
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.types import OrchestratorStalledError, OrchestratorMaxIterationsError
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.environment.pytest_environment import PytestEnvironment
from bizniz.workspace.temp_workspace import TempWorkspace


if __name__ == "__main__":

    config = BiznizConfig.find_and_load()
    client = config.make_client()
    sandbox = DockerExecutionEnvironment()
    workspace = TempWorkspace()
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

    orchestrator = CodingOrchestrator(
        autocoder=Autocoder(client=client, environment=sandbox, workspace=workspace),
        autotester=Autotester(client=client, environment=sandbox, workspace=workspace),
        autodebugger=Autodebugger(client=client, environment=sandbox, workspace=workspace),
        test_environment=pytest_env,
        workspace=workspace,
        client=client,
        client_factory=client_factory,
        debugger_factory=debugger_factory,
        model_progression=config.make_model_progression(),
        max_iterations=config.max_iterations,
        on_status_message=lambda msg: print(f"  [orchestrator] {msg}"),
    )

    # ── Single-file orchestration ─────────────────────────────────────
    print("=== Single-File Orchestration ===")

    try:
        result = orchestrator.run(
            prompt="Write a Python function called 'add' that takes two numbers and returns their sum.",
            code_filename="math_utils.py",
            test_filename="test_math_utils.py",
        )

        if result.success:
            print(f"\nSuccess after {result.iterations} iteration(s)!")
            print(f"Strategy used: {result.strategy_used}")
        else:
            print(f"\nFailed after {result.iterations} iterations.")

    except OrchestratorStalledError as e:
        print(f"\nStalled: {e}")

    except OrchestratorMaxIterationsError as e:
        print(f"\nMax iterations exceeded: {e}")

    # ── Multi-file orchestration ──────────────────────────────────────
    print("\n=== Multi-File Orchestration ===")

    target_files = [
        {"filepath": "calculator/ops.py", "action": "create", "description": "Basic arithmetic operations"},
    ]
    test_files = ["tests/test_ops.py"]

    try:
        result = orchestrator.run_multi(
            prompt="Create a calculator module with add, subtract, multiply, divide functions.",
            target_files=target_files,
            test_files=test_files,
            architecture_context="Simple calculator package.",
        )

        if result.success:
            print(f"\nSuccess after {result.iterations} iteration(s)!")
            print(f"Strategy used: {result.strategy_used}")
            print(f"Files: {[c.filepath for c in result.changes]}")
        else:
            print(f"\nFailed after {result.iterations} iterations.")

    except OrchestratorStalledError as e:
        print(f"\nStalled: {e}")

    except OrchestratorMaxIterationsError as e:
        print(f"\nMax iterations exceeded: {e}")

    print(f"\nWorkspace files: {workspace.tree()}")
