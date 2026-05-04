"""
Integration Debugger Harness

Takes an already-built project (e.g., from auto_architect.py) and runs
ONLY the integration phase with the AgenticDebugger. This lets you
iterate on the debugger without re-paying the engineering cost.

Usage:
    # First, build a project with auto_architect.py
    # Then re-run integration + debugger as many times as you want:
    cd ~/bizniz && set -a && source .env && set +a \
      && PYTHONPATH=. .venv/bin/python -u examples/debug_integration.py \
         ~/bizniz_projects/pet_groomer_v11

    # With extra verbosity on debugger tool calls:
    ... debug_integration.py --verbose ~/bizniz_projects/pet_groomer_v11

    # Skip frontend (backend only):
    ... debug_integration.py --backend-only ~/bizniz_projects/pet_groomer_v11

    # Adjust debugger iterations:
    ... debug_integration.py --max-iterations 5 ~/bizniz_projects/pet_groomer_v11
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ["PYTHONUNBUFFERED"] = "1"

from dotenv import load_dotenv
load_dotenv()

from bizniz.architect.types import (
    ServiceDefinition,
    ServiceResult,
    SystemArchitecture,
)
from bizniz.agents.debugger.agentic import AgenticDebugger
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.integration.http_api_tester import HTTPApiTester
from bizniz.integration.web_ui_tester import WebUITester
from bizniz.integration.runner import run_integration_phase
from bizniz.workspace.local_workspace import LocalWorkspace


_start_time = time.time()


def log(msg: str):
    elapsed = time.time() - _start_time
    print(f"  [{elapsed:6.1f}s] {msg}", flush=True)


PROBLEM_STATEMENT = (
    "Build a web application for a pet grooming salon. "
    "The website should allow customers to: "
    "1) View available grooming services (bath, haircut, nail trim, etc.) with prices, "
    "2) Book an appointment by selecting a service, date, and time slot, "
    "3) View and cancel their existing appointments. "
    "\n\n"
    "The backend should be a REST API with endpoints for services, appointments, "
    "and basic validation (no double-booking the same time slot). "
    "Use in-memory storage for now (no database required)."
)


def reconstruct_architecture(project_root: Path) -> SystemArchitecture:
    """Reconstruct the SystemArchitecture from what's on disk.

    Reads docker-compose.yml to discover services and ports, then
    maps to ServiceDefinitions. For the pet-groomer shape (backend +
    frontend), this is deterministic.
    """
    compose_path = project_root / "infra" / "development" / "docker-compose.yml"
    if not compose_path.exists():
        print(f"  ERROR: {compose_path} not found", flush=True)
        sys.exit(1)

    # Check which service directories exist
    services = []
    if (project_root / "backend").is_dir():
        services.append(ServiceDefinition(
            name="backend",
            service_type="backend",
            framework="fastapi",
            language="python",
            description="REST API for pet grooming services and appointments",
            workspace_name="backend",
            port=8000,
            depends_on=[],
            requirements=["fastapi", "uvicorn"],
            skeleton="fastapi",
        ))
    if (project_root / "frontend").is_dir():
        services.append(ServiceDefinition(
            name="frontend",
            service_type="frontend",
            framework="react",
            language="typescript",
            description="React frontend for pet grooming salon",
            workspace_name="frontend",
            port=5173,
            depends_on=["backend"],
            requirements=[],
            skeleton="react",
        ))

    if not services:
        print("  ERROR: no service directories found in project", flush=True)
        sys.exit(1)

    slug = project_root.name
    return SystemArchitecture(
        project_name=slug,
        project_slug=slug,
        services=services,
        description="Pet grooming web application",
    )


def main():
    parser = argparse.ArgumentParser(description="Integration debugger harness")
    parser.add_argument("project_root", type=Path, help="Path to built project")
    parser.add_argument("--max-iterations", type=int, default=3,
                        help="Max debugger repair iterations per service (default: 3)")
    parser.add_argument("--backend-only", action="store_true",
                        help="Skip frontend integration tests")
    parser.add_argument("--frontend-only", action="store_true",
                        help="Skip backend integration tests")
    parser.add_argument("--verbose", action="store_true",
                        help="Extra logging from debugger tool calls")
    parser.add_argument("--debugger-model", type=str, default=None,
                        help="Override debugger model (default: from bizniz.yaml)")
    parser.add_argument("--skip-test-gen", action="store_true",
                        help="Re-use existing integration test files (skip AI test generation)")
    args = parser.parse_args()

    project_root = args.project_root.expanduser().resolve()
    if not project_root.is_dir():
        print(f"  ERROR: {project_root} is not a directory", flush=True)
        sys.exit(1)

    compose_path = str(project_root / "infra" / "development" / "docker-compose.yml")

    print(f"\n{'='*60}", flush=True)
    print(f"  Integration Debugger Harness", flush=True)
    print(f"{'='*60}\n", flush=True)
    log(f"Project: {project_root}")

    # Load config
    config = BiznizConfig.find_and_load()
    debugger_model = args.debugger_model or config.debugger_model
    log(f"Debugger model: {debugger_model}")
    log(f"Max iterations: {args.max_iterations}")

    # Reconstruct architecture
    architecture = reconstruct_architecture(project_root)
    log(f"Architecture: {len(architecture.services)} services — {', '.join(s.name for s in architecture.services)}")

    # Build workspaces
    service_workspaces = {}
    for svc in architecture.services:
        ws_path = project_root / svc.workspace_name
        if ws_path.is_dir():
            service_workspaces[svc.name] = LocalWorkspace(root=str(ws_path), create=False)
            log(f"  {svc.name}: {ws_path}")

    # Fake service_results as if engineering passed (which it did)
    service_results = [
        ServiceResult(
            service_name=svc.name,
            workspace_name=svc.workspace_name,
            success=True,
            issues_total=4,
            issues_passed=4,
        )
        for svc in architecture.services
        if svc.name in service_workspaces
    ]

    # Factories
    def _make_http_api_tester(workspace):
        return HTTPApiTester(
            client=config.make_integration_tester_client(),
            environment=PythonSandboxExecutionEnvironment(),
            workspace=workspace,
            on_status_message=log,
        )

    def _make_integration_debugger(workspace, model_override: str = None):
        return AgenticDebugger(
            client=config.make_client(model=model_override or debugger_model),
            workspace=workspace,
            environment=PythonSandboxExecutionEnvironment(),
            on_status_message=log,
        )

    # Build escalation chain from config (or skip if --debugger-model
    # override was passed — that signals single-tier intent).
    if args.debugger_model:
        debugger_escalation_specs = None
    else:
        from bizniz.integration.debug_loop import DebuggerTierSpec
        debugger_escalation_specs = [
            DebuggerTierSpec(
                factory=lambda ws, m=t.model: _make_integration_debugger(ws, model_override=m),
                model_label=t.model,
                max_turns=t.max_turns,
                repair_attempts=t.repair_attempts,
            )
            for t in config.debugger_escalation
        ]
        if debugger_escalation_specs:
            log(
                "Debugger escalation: "
                + " → ".join(
                    f"{s.model_label}({s.repair_attempts}×{s.max_turns})"
                    for s in debugger_escalation_specs
                )
            )

    def _make_web_ui_tester(workspace):
        return WebUITester(
            client=config.make_integration_tester_client(),
            environment=PythonSandboxExecutionEnvironment(),
            workspace=workspace,
            on_status_message=log,
        )

    # Filter services based on flags
    if args.backend_only:
        architecture.services = [s for s in architecture.services if s.service_type == "backend"]
        log("Mode: backend-only")
    elif args.frontend_only:
        architecture.services = [s for s in architecture.services if s.service_type == "frontend"]
        log("Mode: frontend-only")

    web_ui_factory = None if args.backend_only else _make_web_ui_tester

    log("Starting integration phase...")
    print(f"\n{'─'*60}", flush=True)

    try:
        results = run_integration_phase(
            architecture=architecture,
            service_results=service_results,
            project_root=project_root,
            problem_statement=PROBLEM_STATEMENT,
            compose_path=compose_path,
            http_api_tester_factory=lambda workspace: _make_http_api_tester(workspace),
            service_workspaces=service_workspaces,
            on_status=log,
            debugger_factory=lambda workspace: _make_integration_debugger(workspace),
            debug_max_iterations=args.max_iterations,
            web_ui_tester_factory=web_ui_factory,
            debugger_escalation=debugger_escalation_specs,
        )
    except KeyboardInterrupt:
        log("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        log(f"INTEGRATION PHASE FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Results
    print(f"\n{'='*60}", flush=True)
    print(f"  Integration Results", flush=True)
    print(f"{'='*60}", flush=True)
    for r in results:
        status = "PASS" if r.success else "FAIL"
        error_info = f" — {r.error[:120]}" if r.error else ""
        print(f"  {r.service_name}: {status}{error_info}", flush=True)

    elapsed = time.time() - _start_time
    print(f"\n  Elapsed: {elapsed:.0f}s", flush=True)

    # Cost summary
    try:
        from bizniz.cost import get_tracker
        cost = get_tracker().summary()
        print(f"\n{'='*60}", flush=True)
        print(f"  Cost", flush=True)
        print(f"{'='*60}", flush=True)
        print("  " + cost.format().replace("\n", "\n  "), flush=True)
    except Exception as e:
        print(f"  Cost summary unavailable: {e}", flush=True)

    all_passed = all(r.success for r in results)
    print(f"\n  Overall: {'PASS' if all_passed else 'FAIL'}", flush=True)
    print(flush=True)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
