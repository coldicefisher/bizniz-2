"""
Example: AutoEngineer

Decomposes a problem statement into structured engineering artifacts
(business requirements, use cases, functional/non-functional requirements,
and coding issues), persists them to a workspace SQLite database, then
dispatches a CodingOrchestrator for each issue.

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
from bizniz.engineer.auto_engineer import AutoEngineer
from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient, ChatGPTClientConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.environment.pytest_environment import PytestEnvironment
from bizniz.workspace.temp_workspace import TempWorkspace
from bizniz.workspace.local_workspace import LocalWorkspace


def make_orchestrator(client, workspace):
    """Factory: returns a fresh CodingOrchestrator per issue."""
    # sandbox = PythonSandboxExecutionEnvironment()
    sandbox = DockerExecutionEnvironment(image="python:3.12-slim")
    pytest_env = PytestEnvironment(workspace_root=workspace.root)

    return CodingOrchestrator(
        autocoder=Autocoder(
            client=client,
            environment=sandbox,
            workspace=workspace,
        ),
        autotester=Autotester(
            client=client,
            environment=sandbox,
            workspace=workspace,
        ),
        test_environment=pytest_env,
        workspace=workspace,
        max_iterations=5,
        on_status_message=lambda msg: print(f"    [orchestrator] {msg}"),
    )


if __name__ == "__main__":

    client = ChatGPTClient(config=ChatGPTClientConfig(default_model="gpt-4o-mini"), api_key=None)
    # workspace = TempWorkspace()
    workspace = LocalWorkspace(root="~/auto_engineer_workspace")

    # ── Step 1: Analyze only (without dispatching) ──────────────────────
    print("=== Analyzing problem statement ===\n")

    with AutoEngineer(
        client=client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=lambda: make_orchestrator(client, workspace),
        on_status_message=lambda msg: print(f"  [engineer] {msg}"),
    ) as engineer:

        analysis = engineer.analyze(
            "Build a command-line expense tracker that lets users add expenses "
            "with a category and amount, list all expenses, and show totals by category."
        )

        print(f"\nProblem ID: {analysis.problem_id}")

        print("\nBusiness Requirements:")
        for req in analysis.requirements:
            if req.type == "business":
                print(f"  - {req.text}")

        print("\nUse Cases:")
        for uc in analysis.use_cases:
            print(f"  - {uc.title}: {uc.description}")

        print("\nFunctional Requirements:")
        for req in analysis.requirements:
            if req.type == "functional":
                print(f"  - {req.text}")

        print("\nNon-Functional Requirements:")
        for req in analysis.requirements:
            if req.type == "nonfunctional":
                print(f"  - {req.text}")

        print("\nIssues:")
        for issue in analysis.issues:
            print(f"  #{issue.db_id}: {issue.title}")
            print(f"         code: {issue.code_file}  tests: {issue.test_file}")

        # ── Step 2: Dispatch a single issue ─────────────────────────────
        # Uncomment to run the full pipeline for the first issue:
        
        # print(f"\n=== Dispatching issue #{analysis.issues[0].db_id} ===\n")
        # result = engineer.dispatch(analysis.issues[0].db_id)
        # print(f"Success: {result.success}, Iterations: {result.iterations}")

        # ── Step 3: Full pipeline (analyze + dispatch all) ──────────────
        # Uncomment to run everything end to end:
        
        results = engineer.run("Build a URL shortener service.")
        for r in results:
            print(f"  Success: {r.success}, Iterations: {r.iterations}")

    print(f"\nWorkspace files: {workspace.tree()}")
