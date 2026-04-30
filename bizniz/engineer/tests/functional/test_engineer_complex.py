"""
Functional test: high complexity — inventory management system.

Multiple interrelated entities, business logic with validation,
reporting/aggregation, and cross-module dependencies.

Run with:
    pytest bizniz/engineer/tests/functional/test_engineer_complex.py -m functional -v
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


def _make_orchestrator(config, workspace, suggested_model=None, image_name=None, on_status_message=None):
    sandbox = DockerExecutionEnvironment()
    test_env = DockerPytestEnvironment(
        workspace_root=workspace.root,
        image=image_name or "bizniz-python-runner",
    )

    def debugger_factory():
        fresh_client = config.make_client()
        return AgenticDebugger(
            client=fresh_client, workspace=workspace, environment=test_env,
            on_status_message=on_status_message,
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
        on_status_message=on_status_message,
    )


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


@pytest.mark.functional
def test_inventory_system_full_pipeline(api_key, workspace_path):
    """High complexity: inventory system with products, stock tracking, and reporting."""
    config = BiznizConfig(api_key=api_key)
    engineer_client = config.make_engineer_client()
    workspace = LocalWorkspace(root=str(workspace_path))

    import time as _time
    _t0 = _time.time()

    def _log(msg):
        elapsed = _time.time() - _t0
        print(f"  [{elapsed:6.1f}s] {msg}", flush=True)

    with Engineer(
        client=engineer_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=lambda suggested_model=None, image_name=None: _make_orchestrator(
            config, workspace, suggested_model=suggested_model, image_name=image_name,
            on_status_message=_log,
        ),
        on_status_message=_log,
    ) as engineer:

        analysis = engineer.analyze(PROBLEM_STATEMENT)

        assert analysis.problem_id is not None
        assert analysis.architecture is not None
        assert len(analysis.architecture.namespaces) >= 2, (
            f"Expected at least 2 namespaces (models + service), "
            f"got {len(analysis.architecture.namespaces)}"
        )
        assert len(analysis.issues) >= 3, (
            f"Expected at least 3 issues for inventory system, got {len(analysis.issues)}"
        )

        # Should have domain models planned
        assert len(analysis.architecture.domain_models) >= 1, (
            f"Expected at least 1 domain model, got {len(analysis.architecture.domain_models)}"
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
    total_iters = sum(r.iterations for r in results)
    print(f"\n  Inventory system: {len(successes)}/{total} issues resolved, "
          f"{total_iters} total iterations")

    # Most issues should succeed
    assert len(successes) >= total // 2, (
        f"Too many failures: {len(successes)}/{total} passed"
    )

    # Verify workspace structure
    files = [str(f) for f in workspace.list_relative_files()]
    py_source = [f for f in files if f.endswith(".py") and not f.startswith("tests/") and not f.startswith(".")]
    py_tests = [f for f in files if f.endswith(".py") and f.startswith("tests/")]
    assert len(py_source) >= 2, f"Expected at least 2 source files, got: {py_source}"
    assert len(py_tests) >= 1, f"Expected at least 1 test file, got: {py_tests}"
