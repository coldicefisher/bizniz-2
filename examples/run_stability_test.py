"""
Stability Test: Run Engineer N times consecutively.

Tracks success/failure across runs and reports aggregate stats.
Stops on first failure by default (use --continue-on-failure to keep going).

Usage:
    python examples/run_stability_test.py [--runs N] [--continue-on-failure]
"""
import os
import sys
import shutil
import argparse
import datetime
import json

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
from bizniz.logging.pipeline_logger import PipelineLogger


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
        on_status_message=lambda msg: print(f"      [orchestrator] {msg}"),
    )


def run_once(run_number: int, config: BiznizConfig) -> dict:
    """Execute one full engineer run. Returns a result dict."""
    workspace_path = os.path.expanduser("~/engineer_workspace")
    if os.path.exists(workspace_path):
        shutil.rmtree(workspace_path)

    workspace = LocalWorkspace(root=workspace_path)
    log_dir = os.path.expanduser("~/.bizniz_stability_logs")
    logger = PipelineLogger(log_dir=log_dir, run_id=f"stability_{run_number}_{datetime.datetime.now().strftime('%H%M%S')}")

    engineer_client = config.make_engineer_client()
    logger.log_run_start(PROBLEM_STATEMENT)

    result_info = {
        "run": run_number,
        "success": False,
        "total_issues": 0,
        "resolved": 0,
        "failed": 0,
        "total_iterations": 0,
        "error": None,
        "log_path": str(logger.log_path),
    }

    try:
        with Engineer(
            client=engineer_client,
            environment=PythonSandboxExecutionEnvironment(),
            workspace=workspace,
            orchestrator_factory=lambda suggested_model=None, image_name=None: _make_orchestrator(
                config, workspace, suggested_model=suggested_model, image_name=image_name,
            ),
            on_status_message=lambda msg: print(f"    [engineer] {msg}"),
        ) as engineer:

            analysis = engineer.analyze(PROBLEM_STATEMENT)

            print(f"    Issues: {len(analysis.issues)}")
            for issue in analysis.issues:
                model_tag = f" [{issue.suggested_model}]" if issue.suggested_model else ""
                print(f"      #{issue.db_id}: {issue.title}{model_tag}")

            resolved = 0
            failed = 0
            total_iterations = 0

            for issue in analysis.issues:
                print(f"    Dispatching #{issue.db_id}: {issue.title}")
                logger.log_issue_start(issue.db_id, issue.title, issue.suggested_model)

                try:
                    result = engineer.dispatch(issue.db_id)
                    logger.log_issue_end(issue.db_id, result.success, result.iterations)
                    total_iterations += result.iterations

                    if result.success:
                        resolved += 1
                        print(f"      OK ({result.iterations} iterations)")
                    else:
                        failed += 1
                        print(f"      FAILED ({result.iterations} iterations)")
                        logger.log_error(issue.db_id, "issue_failed", f"Failed after {result.iterations} iterations")
                except Exception as e:
                    failed += 1
                    error_msg = f"{type(e).__name__}: {e}"
                    print(f"      CRASHED: {error_msg}")
                    logger.log_error(issue.db_id, type(e).__name__, error_msg)
                    logger.log_issue_end(issue.db_id, False, 0)

            result_info["total_issues"] = len(analysis.issues)
            result_info["resolved"] = resolved
            result_info["failed"] = failed
            result_info["total_iterations"] = total_iterations
            result_info["success"] = failed == 0

    except Exception as e:
        result_info["error"] = f"{type(e).__name__}: {e}"
        print(f"    RUN CRASHED: {result_info['error']}")
        logger.log_error(None, type(e).__name__, str(e))

    logger.log_run_end(
        success=result_info["success"],
        total_issues=result_info["total_issues"],
        resolved=result_info["resolved"],
        failed=result_info["failed"],
    )

    return result_info


def main():
    parser = argparse.ArgumentParser(description="Run Engineer stability test")
    parser.add_argument("--runs", type=int, default=5, help="Number of consecutive runs (default: 5)")
    parser.add_argument("--continue-on-failure", action="store_true", help="Keep running after a failure")
    args = parser.parse_args()

    config = BiznizConfig.find_and_load()
    results = []
    consecutive_passes = 0

    print(f"=== Stability Test: {args.runs} consecutive runs ===\n")
    print(f"  Engineer model: {config.engineer_model}")
    print(f"  Default model: {config.default_model}")
    print(f"  Model progression: {config.models}")
    print(f"  Max iterations: {config.max_iterations}")
    print()

    for run in range(1, args.runs + 1):
        print(f"--- Run {run}/{args.runs} ---")
        result = run_once(run, config)
        results.append(result)

        if result["success"]:
            consecutive_passes += 1
            print(f"  PASS (consecutive: {consecutive_passes})\n")
        else:
            consecutive_passes = 0
            print(f"  FAIL\n")
            if not args.continue_on_failure:
                print("  Stopping on first failure. Use --continue-on-failure to keep going.")
                break

    # Summary
    total = len(results)
    passes = sum(1 for r in results if r["success"])
    fails = total - passes
    total_iterations = sum(r["total_iterations"] for r in results)

    print(f"\n{'=' * 50}")
    print(f"STABILITY TEST RESULTS")
    print(f"{'=' * 50}")
    print(f"  Runs: {total}")
    print(f"  Passes: {passes}")
    print(f"  Fails: {fails}")
    print(f"  Pass rate: {passes/total*100:.0f}%")
    print(f"  Total iterations across all runs: {total_iterations}")
    print()

    for r in results:
        status = "PASS" if r["success"] else "FAIL"
        error = f" — {r['error']}" if r.get("error") else ""
        print(f"  Run {r['run']}: {status} | {r['resolved']}/{r['total_issues']} issues | {r['total_iterations']} iterations{error}")
        print(f"    Log: {r['log_path']}")

    # Save aggregate results
    log_dir = os.path.expanduser("~/.bizniz_stability_logs")
    os.makedirs(log_dir, exist_ok=True)
    summary_path = os.path.join(log_dir, f"stability_summary_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(summary_path, "w") as f:
        json.dump({"results": results, "passes": passes, "fails": fails, "total": total}, f, indent=2)
    print(f"\n  Summary saved: {summary_path}")

    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
