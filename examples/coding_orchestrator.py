"""
Example: CodingOrchestrator

Iteratively generates code + tests, runs pytest, and repairs until tests pass.
Uses TDD by default (tests first, then code).
Tests run inside Docker containers via DockerPytestEnvironment.

Requirements:
    - OPENAI_API_KEY environment variable set (or .env file)
    - Docker daemon running
"""
from dotenv import load_dotenv

load_dotenv()

from bizniz.agents.coder.coder import Coder
from bizniz.agents.debugger.quick import QuickDebugger
from bizniz.agents.debugger.agentic import AgenticDebugger
from bizniz.tester.tester import Tester
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.types import OrchestratorStalledError, OrchestratorMaxIterationsError
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
from bizniz.workspace.temp_workspace import TempWorkspace


if __name__ == "__main__":

    config = BiznizConfig.find_and_load()
    client = config.make_client()
    sandbox = DockerExecutionEnvironment()
    workspace = TempWorkspace()

    # Tests run inside a Docker container with Python + pytest
    test_env = DockerPytestEnvironment(
        workspace_root=workspace.root,
        image="bizniz-python-runner",
    )

    def debugger_factory():
        fresh_client = config.make_client()
        return AgenticDebugger(
            client=fresh_client,
            workspace=workspace,
            environment=test_env,
        )

    def client_factory(model_name):
        return config.make_client(model=model_name)

    orchestrator = CodingOrchestrator(
        coder=Coder(client=client, environment=sandbox, workspace=workspace),
        tester=Tester(client=client, environment=sandbox, workspace=workspace),
        quick_debugger=QuickDebugger(client=client, environment=sandbox, workspace=workspace),
        test_environment=test_env,
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

    print(f"\nWorkspace files: {workspace.tree()}")
