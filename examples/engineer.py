"""
Example: Engineer — full pipeline

Decomposes a problem statement into engineering artifacts (requirements,
architecture, issues), then dispatches a CodingOrchestrator per issue.

This example uses the most complex problem: an inventory management system
with multiple domain models, business logic, and cross-module dependencies.

Requirements:
    - OPENAI_API_KEY environment variable set (or .env file)
    - Docker daemon running
"""
import os
import shutil

from dotenv import load_dotenv

load_dotenv()

from bizniz.agents.coder.coder import Coder
from bizniz.agents.debugger.quick import QuickDebugger
from bizniz.agents.debugger.agentic import AgenticDebugger
from bizniz.tester.tester import Tester
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.engineer.engineer import Engineer
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
from bizniz.workspace.local_workspace import LocalWorkspace


PROBLEM_STATEMENT = (
    "Build a Python inventory management system for a small warehouse. "
    "\n\n"
    "Domain models:\n"
    "- Product: has name (str), sku (str, unique), price (float), category (str)\n"
    "- StockEntry: tracks product_sku (str), quantity (int), and "
    "  timestamp (datetime) for each stock-in or stock-out event\n"
    "\n"
    "Core service (InventoryManager):\n"
    "- register_product(name, sku, price, category) — add a new product, "
    "  raise ValueError if SKU already exists\n"
    "- stock_in(sku, quantity) — record incoming stock for a product\n"
    "- stock_out(sku, quantity) — record outgoing stock, raise ValueError "
    "  if insufficient stock\n"
    "- get_stock_level(sku) — return current quantity (sum of all stock entries)\n"
    "- get_products_by_category(category) — return list of products in a category\n"
    "- get_low_stock_products(threshold) — return products with stock below threshold\n"
    "- get_inventory_value() — return total value (price * quantity) across all products\n"
    "\n"
    "Use in-memory storage (lists/dicts). No database or file I/O."
)

# Default Docker image for running tests (has Python + pytest)
DEFAULT_IMAGE = "bizniz-python-runner"


def _make_orchestrator(config, workspace, on_status_message=None, suggested_model=None, image_name=None):
    """Factory: returns a fresh CodingOrchestrator per issue."""
    sandbox = DockerExecutionEnvironment()
    test_env = DockerPytestEnvironment(
        workspace_root=workspace.root,
        image=image_name or DEFAULT_IMAGE,
    )

    def debugger_factory():
        fresh_client = config.make_client(model=config.debugger_model)
        return AgenticDebugger(
            client=fresh_client,
            workspace=workspace,
            environment=test_env,
            on_status_message=on_status_message,
        )

    def client_factory(model_name):
        return config.make_client(model=model_name)

    issue_client = config.make_client(model=suggested_model or config.engineer_model)

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
        on_status_message=on_status_message,
    )


if __name__ == "__main__":

    config = BiznizConfig.find_and_load()
    engineer_client = config.make_engineer_client()

    # Clean workspace on every run for a fresh start
    workspace_path = os.path.expanduser("~/engineer_workspace")
    if os.path.exists(workspace_path):
        for root, dirs, files in os.walk(workspace_path):
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), 0o666)
                except OSError:
                    pass
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o777)
                except OSError:
                    pass
        shutil.rmtree(workspace_path)

    workspace = LocalWorkspace(root=workspace_path)

    # ── Step 1: Analyze ───────────────────────────────────────────────
    print("=== Analyzing problem statement ===\n")

    with Engineer(
        client=engineer_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=lambda suggested_model=None, on_status_message=None, image_name=None: _make_orchestrator(
            config, workspace,
            on_status_message=on_status_message,
            suggested_model=suggested_model,
            image_name=image_name,
        ),
        on_status_message=lambda msg: print(f"  [engineer] {msg}"),
    ) as engineer:

        analysis = engineer.analyze(PROBLEM_STATEMENT)

        print(f"Problem ID: {analysis.problem_id}")

        # Architecture
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

        # Issues
        print(f"\nIssues ({len(analysis.issues)}):")
        for issue in analysis.issues:
            model_tag = f" [{issue.suggested_model}]" if issue.suggested_model else ""
            targets = ", ".join(tf.filepath for tf in issue.target_files)
            tests = ", ".join(issue.test_files)
            print(f"  #{issue.db_id}: {issue.title}{model_tag}")
            print(f"         target: {targets}  tests: {tests}")

        # ── Step 2: Dispatch all issues ───────────────────────────────
        print(f"\n=== Dispatching {len(analysis.issues)} issue(s) ===\n")

        results = []
        for issue in analysis.issues:
            print(f"  Dispatching #{issue.db_id}: {issue.title}")
            result = engineer.dispatch(issue.db_id)
            results.append(result)
            status = "PASS" if result.success else "FAIL"
            print(f"    {status} ({result.iterations} iterations)")

    # ── Summary ───────────────────────────────────────────────────────
    successes = [r for r in results if r.success]
    total_iters = sum(r.iterations for r in results)

    print(f"\n=== Results ===")
    print(f"  {len(successes)}/{len(results)} issues resolved")
    print(f"  {total_iters} total iterations")
    print(f"\nWorkspace files: {workspace.tree()}")
