"""
Example: CodingOrchestrator

Iteratively generates code + tests, runs pytest, and repairs until tests pass.
Combines Autocoder (code generation) and Autotester (test generation) with a
PytestEnvironment for test execution.

Requirements:
    - OPENAI_API_KEY environment variable set
"""
import os
import shutil

from dotenv import load_dotenv

load_dotenv()  # automatically finds .env in current directory or parents



from bizniz.autocoder.autocoder import Autocoder
from bizniz.autotester.autotester import Autotester
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.types import OrchestratorStalledError, OrchestratorMaxIterationsError
from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient, ChatGPTClientConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.environment.pytest_environment import PytestEnvironment
from bizniz.workspace.temp_workspace import TempWorkspace


if __name__ == "__main__":

    # Shared dependencies
    client = ChatGPTClient(config=ChatGPTClientConfig(default_model="gpt-4o-mini"), api_key=None)
    sandbox = PythonSandboxExecutionEnvironment()
    workspace = TempWorkspace()
    pytest_env = PytestEnvironment(workspace_root=workspace.root)

    # Create the agents
    autocoder = Autocoder(
        client=client,
        environment=sandbox,
        workspace=workspace,
    )

    autotester = Autotester(
        client=client,
        environment=sandbox,
        workspace=workspace,
    )

    # Create the orchestrator
    orchestrator = CodingOrchestrator(
        autocoder=autocoder,
        autotester=autotester,
        test_environment=pytest_env,
        workspace=workspace,
        max_iterations=10,
        on_status_message=lambda msg: print(f"  [orchestrator] {msg}"),
    )

    # Run the full loop
    prompt = (
        "Write a function called roman_to_int(s: str) -> int that converts "
        "a Roman numeral string to an integer. Support I, V, X, L, C, D, M."
    )

    try:
        result = orchestrator.run(
            prompt=prompt,
            code_filename="roman_to_int.py",
            test_filename="test_roman_to_int.py",
        )

        if result.success:
            print(f"\nSuccess after {result.iterations} iteration(s)!")
            print("\n=== Code ===")
            print(result.code)
            print("\n=== Tests ===")
            print(result.tests)
        else:
            print(f"\nFailed after {result.iterations} iterations.")
            print(f"Error: {result.error}")

    except OrchestratorStalledError as e:
        print(f"\nStalled: {e}")

    except OrchestratorMaxIterationsError as e:
        print(f"\nMax iterations exceeded: {e}")

    # Optionally strengthen the tests
    print("\n=== Strengthening tests ===")
    orchestrator.strengthen_tests(
        code_filename="roman_to_int.py",
        test_filename="test_roman_to_int.py",
        output_filename="test_roman_to_int_v2.py",
    )

    print(f"\nWorkspace files: {workspace.tree()}")
