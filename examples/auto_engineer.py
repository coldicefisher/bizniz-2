"""
Example: AutoEngineer

Decomposes a problem statement into structured engineering artifacts
(business requirements, use cases, functional/non-functional requirements,
architecture plan, and coding issues), persists them to a workspace SQLite
database, then dispatches a CodingOrchestrator for each issue.

The full pipeline:
1. analyze() — AI produces requirements, use cases, architecture plan, and issues
2. Package structure is created (pyproject.toml, namespaces, __init__.py files)
3. dispatch() per issue — orchestrator generates code + tests across multiple files
4. Governance loop — if autocoder creates unplanned files, the engineer reviews
   the drift and approves, rejects, or modifies the architecture plan

Requirements:
    - OPENAI_API_KEY environment variable set
"""
import os
import shutil

from dotenv import load_dotenv

load_dotenv()  # automatically finds .env in current directory or parents


from bizniz.autocoder.autocoder import Autocoder
from bizniz.autodebugger.autodebugger import Autodebugger
from bizniz.autotester.autotester import Autotester
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.model_progression import ModelProgression
from bizniz.engineer.auto_engineer import AutoEngineer
from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient, ChatGPTClientConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.environment.pytest_environment import PytestEnvironment
from bizniz.workspace.local_workspace import LocalWorkspace


def make_orchestrator(client, workspace):
    """Factory: returns a fresh CodingOrchestrator per issue."""
    sandbox = DockerExecutionEnvironment()
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
        autodebugger=Autodebugger(
            client=client,
            environment=sandbox,
            workspace=workspace,
        ),
        test_environment=pytest_env,
        workspace=workspace,
        client=client,
        model_progression=ModelProgression(),
        max_iterations=20,
        on_status_message=lambda msg: print(f"    [orchestrator] {msg}"),
    )


if __name__ == "__main__":

    client = ChatGPTClient(config=ChatGPTClientConfig(default_model="gpt-4o-mini"), api_key=None)

    # Clean workspace on every run for a fresh start
    workspace_path = os.path.expanduser("~/auto_engineer_workspace")
    if os.path.exists(workspace_path):
        shutil.rmtree(workspace_path)

    workspace = LocalWorkspace(root=workspace_path)

    # ── Step 1: Analyze (requirements + architecture + issues) ────────
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

        # ── Architecture Plan ─────────────────────────────────────────
        if analysis.architecture:
            arch = analysis.architecture
            print(f"\nArchitecture Plan:")
            print(f"  Package: {arch.package_name}")
            print(f"  Namespaces:")
            for ns in arch.namespaces:
                print(f"    - {ns.namespace_path}: {ns.purpose}")
            if arch.domain_models:
                print(f"  Domain Models:")
                for dm in arch.domain_models:
                    fields = ", ".join(f"{f.name}: {f.type_hint}" for f in dm.fields)
                    print(f"    - {dm.class_name} ({dm.filepath}): {fields}")
            if arch.modules:
                print(f"  Modules:")
                for mod in arch.modules:
                    name = mod.class_name or "(module-level)"
                    methods = ", ".join(m.name for m in mod.methods)
                    print(f"    - {name} ({mod.filepath}): {methods}")
            if arch.dependencies:
                print(f"  Dependencies:")
                for dep in arch.dependencies:
                    symbols = ", ".join(dep.import_symbols)
                    print(f"    - {dep.source_filepath} → {dep.target_filepath} [{symbols}]")

        # ── Requirements ──────────────────────────────────────────────
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

        # ── Issues ────────────────────────────────────────────────────
        print("\nIssues:")
        for issue in analysis.issues:
            print(f"  #{issue.db_id}: {issue.title}")
            targets = ", ".join(tf.filepath for tf in issue.target_files)
            tests = ", ".join(issue.test_files)
            print(f"         target: {targets}  tests: {tests}")

        # ── Step 2: Dispatch all analyzed issues ──────────────────────
        print(f"\n=== Dispatching {len(analysis.issues)} issue(s) ===\n")
        for issue in analysis.issues:
            print(f"  Dispatching issue #{issue.db_id}: {issue.title}")
            result = engineer.dispatch(issue.db_id)
            print(f"    Success: {result.success}, Iterations: {result.iterations}")
            if result.architecture_drift_detected:
                print(f"    Drift detected: {result.drift_files}")

    print(f"\nWorkspace files: {workspace.tree()}")
