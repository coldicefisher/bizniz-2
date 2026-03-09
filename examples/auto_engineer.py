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
from bizniz.agentic_debugger.agentic_debugger import AgenticDebugger
from bizniz.autotester.autotester import Autotester
from bizniz.deep_debugger.deep_debugger import DeepDebugger
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.engineer.auto_engineer import AutoEngineer
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.environment.pytest_environment import PytestEnvironment
from bizniz.workspace.local_workspace import LocalWorkspace
from bizniz.logging.pipeline_logger import PipelineLogger


def make_orchestrator(client, workspace, config, suggested_model=None):
    """Factory: returns a fresh CodingOrchestrator per issue."""
    sandbox = DockerExecutionEnvironment()
    pytest_env = PytestEnvironment(workspace_root=workspace.root)

    def debugger_factory():
        """Create an AgenticDebugger with its own fresh client instance."""
        fresh_client = config.make_client()
        return AgenticDebugger(
            client=fresh_client,
            workspace=workspace,
            environment=pytest_env,
            on_status_message=lambda msg: print(f"    [debugger] {msg}"),
        )

    def deep_debugger_factory():
        """Create a DeepDebugger with its own fresh client instance."""
        fresh_client = config.make_client()
        return DeepDebugger(
            client=fresh_client,
            on_status_message=lambda msg: print(f"    [deep-debugger] {msg}"),
        )

    def client_factory(model_name):
        """Create a fresh client for a specific model (used on escalation)."""
        return config.make_client(model=model_name)

    # Use suggested_model for this issue's starting client
    issue_client = config.make_client(model=suggested_model) if suggested_model else client

    return CodingOrchestrator(
        autocoder=Autocoder(
            client=issue_client,
            environment=sandbox,
            workspace=workspace,
        ),
        autotester=Autotester(
            client=issue_client,
            environment=sandbox,
            workspace=workspace,
        ),
        autodebugger=Autodebugger(
            client=issue_client,
            environment=sandbox,
            workspace=workspace,
        ),
        test_environment=pytest_env,
        workspace=workspace,
        client=issue_client,
        client_factory=client_factory,
        debugger_factory=debugger_factory,
        deep_debugger_factory=deep_debugger_factory,
        model_progression=config.make_model_progression(),
        max_iterations=config.max_iterations,
        on_status_message=lambda msg: print(f"    [orchestrator] {msg}"),
    )


if __name__ == "__main__":

    config = BiznizConfig.find_and_load()

    # Engineer uses the best available model for analysis + planning
    engineer_client = config.make_engineer_client()

    # Clean workspace on every run for a fresh start
    workspace_path = os.path.expanduser("~/auto_engineer_workspace")
    if os.path.exists(workspace_path):
        # Fix permissions before rmtree — Docker may create read-only files
        for root, dirs, files in os.walk(workspace_path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    os.chmod(fp, 0o666)
                except OSError:
                    pass
            for d in dirs:
                dp = os.path.join(root, d)
                try:
                    os.chmod(dp, 0o777)
                except OSError:
                    pass
        shutil.rmtree(workspace_path)

    workspace = LocalWorkspace(root=workspace_path)

    # Set up structured logging
    log_dir = os.path.join(workspace_path, ".bizniz", "logs")
    logger = PipelineLogger(log_dir=log_dir)

    # ── Step 1: Analyze (requirements + architecture + issues) ────────
    print("=== Analyzing problem statement ===\n")

    problem_statement = (
        "Build a command-line expense tracker that lets users add expenses "
        "with a category and amount, list all expenses, and show totals by category."
    )
    logger.log_run_start(problem_statement)

    with AutoEngineer(
        client=engineer_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=lambda suggested_model=None: make_orchestrator(
            engineer_client, workspace, config, suggested_model=suggested_model,
        ),
        on_status_message=lambda msg: print(f"  [engineer] {msg}"),
    ) as engineer:

        analysis = engineer.analyze(problem_statement)

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
            model_tag = f" [model: {issue.suggested_model}]" if issue.suggested_model else ""
            print(f"  #{issue.db_id}: {issue.title}{model_tag}")
            targets = ", ".join(tf.filepath for tf in issue.target_files)
            tests = ", ".join(issue.test_files)
            print(f"         target: {targets}  tests: {tests}")

        # ── Step 2: Dispatch all analyzed issues ──────────────────────
        print(f"\n=== Dispatching {len(analysis.issues)} issue(s) ===\n")
        resolved = 0
        failed = 0
        for issue in analysis.issues:
            print(f"  Dispatching issue #{issue.db_id}: {issue.title}")
            logger.log_issue_start(issue.db_id, issue.title, issue.suggested_model)
            result = engineer.dispatch(issue.db_id)
            logger.log_issue_end(issue.db_id, result.success, result.iterations)
            print(f"    Success: {result.success}, Iterations: {result.iterations}")
            if result.success:
                resolved += 1
            else:
                failed += 1
                logger.log_error(issue.db_id, "issue_failed", f"Issue #{issue.db_id} failed after {result.iterations} iterations")
            if result.architecture_drift_detected:
                print(f"    Drift detected: {result.drift_files}")

    logger.log_run_end(
        success=failed == 0,
        total_issues=len(analysis.issues),
        resolved=resolved,
        failed=failed,
    )

    summary = logger.get_summary()
    print(f"\n=== Run Summary ===")
    print(f"  Run ID: {summary['run_id']}")
    print(f"  Issues: {summary['total_issues']} total, {summary['resolved']} resolved, {summary['failed']} failed")
    print(f"  Total iterations: {summary['total_iterations']}")
    print(f"  Escalations: {summary['escalations']}, Stalls: {summary['stalls']}")
    print(f"  Log: {summary['log_path']}")

    print(f"\nWorkspace files: {workspace.tree()}")
