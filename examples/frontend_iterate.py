"""Frontend-only iteration harness — generate + run + debug Playwright
tests against a project whose backend is already in a working state.

Use this to flush out frontend bugs without re-running the full M1
pipeline (which costs $0.50-$1.50 per cycle and takes 30+ minutes).
The backend stays put; we only iterate on frontend code + tests.

Usage:
    PYTHONPATH=. .venv/bin/python -u examples/frontend_iterate.py \\
        ~/bizniz_projects/property_manager_v1 \\
        --max-iterations 5

What it does:
    1. Bring up the project's full stack (idempotent if already up)
    2. Verify backend health
    3. Generate Playwright tests via WebUITester (using backend OpenAPI
       contract from disk + AUTH_CONTRACT.md for real auth flows)
    4. Run tests via the bizniz-test-playwright sidecar
    5. On failure: dispatch the integration agentic debugger
    6. Loop up to ``max_iterations`` times via the existing escalation chain

What it does NOT do:
    - Re-engineer the backend or the frontend (no Engineer dispatch)
    - Tear down the stack at the end (deliberate — keep it up for
      manual verification afterward)
    - Run backend HTTP integration tests (assumed already passing)
"""
from __future__ import annotations

import argparse
import json as _json
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env from the bizniz repo root so callers don't need to remember
# ``set -a && source .env && set +a`` before invoking. Mirrors what
# the e2e/run.sh wrapper scripts do.
def _load_env():
    import os
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
_load_env()

from bizniz.architect.types import ServiceDefinition, ServiceResult
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.integration.runner import (
    _load_auth_contract_for_compose,
    _run_playwright_in_sidecar,
)
from bizniz.integration.web_ui_tester import WebUITester
from bizniz.integration.debug_loop import (
    DebuggerTierSpec,
    repair_integration_failure,
)
from bizniz.workspace.local_workspace import LocalWorkspace
from bizniz.agents.debugger.agentic import AgenticDebugger


_start_time = time.time()


def log(msg: str):
    elapsed = time.time() - _start_time
    print(f"  [{elapsed:6.1f}s] {msg}", flush=True)


def _wait_backend_ready(deadline_s: float = 120.0) -> bool:
    """Block until backend's /health responds 200 OR deadline expires."""
    end = time.monotonic() + deadline_s
    import requests
    while time.monotonic() < end:
        try:
            r = requests.get("http://localhost:8000/health", timeout=3.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2.0)
    return False


def main():
    parser = argparse.ArgumentParser(description="Frontend-only iteration harness")
    parser.add_argument("project_root", type=Path)
    parser.add_argument("--max-iterations", type=int, default=5,
                        help="Max debugger iterations after first failure "
                             "(only used if bizniz.yaml has no escalation tiers)")
    parser.add_argument("--skip-test-gen", action="store_true",
                        help="Use the existing tests/integration/test_app.spec.cjs "
                             "without regenerating; skip straight to running them")
    parser.add_argument("--problem-statement", type=str, default=None,
                        help="Override problem statement passed to WebUITester. "
                             "Defaults to reading from <project>/docs/plan.json.")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    if not project_root.is_dir():
        print(f"ERROR: project root not found: {project_root}")
        sys.exit(1)

    compose_path = str(project_root / "infra" / "development" / "docker-compose.yml")
    if not Path(compose_path).is_file():
        print(f"ERROR: docker-compose.yml not at {compose_path}")
        sys.exit(1)

    frontend_root = project_root / "frontend"
    if not frontend_root.is_dir():
        print(f"ERROR: frontend dir not at {frontend_root}")
        sys.exit(1)

    log(f"Project: {project_root}")
    log(f"Frontend: {frontend_root}")
    log(f"Max iterations: {args.max_iterations}")

    # Bring up stack (idempotent if already up)
    log("Bringing up stack...")
    up = subprocess.run(
        ["docker", "compose", "-f", compose_path, "up", "-d"],
        capture_output=True, text=True, timeout=600,
    )
    if up.returncode != 0:
        log(f"compose up failed: {up.stderr.strip()[:300]}")
        sys.exit(1)

    log("Waiting for backend health...")
    if not _wait_backend_ready(deadline_s=120.0):
        log("Backend never came up at http://localhost:8000/health — aborting")
        sys.exit(1)
    log("Backend is healthy")

    # Verify AUTH_CONTRACT.md exists (testers need it for real auth flows)
    auth_contract = _load_auth_contract_for_compose(compose_path)
    if auth_contract:
        log("AUTH_CONTRACT.md loaded — testers will drive real auth flows")
    else:
        log("WARN: no AUTH_CONTRACT.md found — testers may not authenticate")

    # Backend OpenAPI from a prior run, used as the API contract the
    # frontend tests against.
    contract_path = project_root / "contracts" / "backend.openapi.json"
    backend_contracts: dict = {}
    if contract_path.is_file():
        try:
            backend_contracts = {"backend": _json.loads(contract_path.read_text())}
            log(f"Loaded backend OpenAPI contract")
        except Exception as e:
            log(f"WARN: contract read failed ({e}) — testers will work without it")
    else:
        log(f"WARN: {contract_path} missing — capture by running M1 backend phase first")

    # Problem statement: from CLI override, or from plan.json
    if args.problem_statement:
        problem_statement = args.problem_statement
    else:
        plan_path = project_root / "docs" / "plan.json"
        if plan_path.is_file():
            try:
                plan = _json.loads(plan_path.read_text())
                problem_statement = plan.get("problem_statement") or ""
                log(f"Loaded problem statement from plan.json ({len(problem_statement)} chars)")
            except Exception as e:
                log(f"WARN: plan.json read failed ({e}) — using empty problem statement")
                problem_statement = ""
        else:
            problem_statement = ""

    # Frontend service definition + workspace
    frontend_service = ServiceDefinition(
        name="frontend",
        service_type="frontend",
        framework="react",
        language="typescript",
        description="Frontend (iterate-mode)",
        workspace_name="frontend",
        port=5173,
        depends_on=["backend"],
        requirements=[],
        skeleton="react",
    )
    frontend_ws = LocalWorkspace(root=str(frontend_root), create=False)

    config = BiznizConfig.find_and_load()
    log(f"Integration tester model: {config.integration_tester_model}")
    log(f"Debugger model: {config.debugger_model}")

    # Generate Playwright tests (or reuse existing)
    target_rel = "tests/integration/test_app.spec.cjs"
    target_abs = frontend_root / target_rel
    if args.skip_test_gen and target_abs.is_file():
        log(f"Using existing {target_rel}")
    else:
        log("Generating Playwright tests via WebUITester...")
        tester = WebUITester(
            client=config.make_integration_tester_client(),
            environment=PythonSandboxExecutionEnvironment(),
            workspace=frontend_ws,
            on_status_message=log,
        )
        test_source = tester.generate_test_file(
            problem_statement=problem_statement,
            service=frontend_service,
            backend_contracts=backend_contracts,
            target_filepath=target_rel,
            auth_contract=auth_contract or "",
        )
        # Tester returns the source string — caller writes it.
        target_abs.parent.mkdir(parents=True, exist_ok=True)
        target_abs.write_text(test_source)
        log(f"Generated {target_rel} ({len(test_source)} bytes)")

    # Build escalation specs from config — same chain full M1 uses.
    escalation: List[DebuggerTierSpec] = []
    for tier in (config.debugger_escalation or []):
        def _factory(ws, _model=tier.model):
            return AgenticDebugger(
                client=config.make_client(model=_model),
                workspace=ws,
                environment=PythonSandboxExecutionEnvironment(),
                on_status_message=log,
                compose_path=compose_path,
                service_name=frontend_service.name,
            )
        escalation.append(DebuggerTierSpec(
            factory=_factory,
            model_label=tier.model,
            tool_iterations=tier.tool_iterations,
            repair_attempts=tier.repair_attempts,
        ))
    if escalation:
        chain = " → ".join(
            f"{s.model_label}({s.repair_attempts}×{s.tool_iterations})" for s in escalation
        )
        log(f"Debugger escalation: {chain}")
    else:
        # Fallback: single tier from --max-iterations
        log(f"No escalation tiers in bizniz.yaml — using single tier × {args.max_iterations}")
        escalation = [DebuggerTierSpec(
            factory=lambda ws: AgenticDebugger(
                client=config.make_client(model=config.debugger_model),
                workspace=ws,
                environment=PythonSandboxExecutionEnvironment(),
                on_status_message=log,
                compose_path=compose_path,
                service_name=frontend_service.name,
            ),
            model_label=config.debugger_model,
            tool_iterations=12,
            repair_attempts=args.max_iterations,
        )]

    # Build the rerun callback
    def _rerun_playwright():
        return _run_playwright_in_sidecar(
            service=frontend_service,
            workspace_path=frontend_root,
            compose_path=compose_path,
            on_status=log,
            timeout_s=180.0,
        )

    # First run
    log("Running Playwright tests (initial pass)...")
    ok, output = _rerun_playwright()
    if ok:
        log("✓ Frontend Playwright tests PASSED on first run")
        sys.exit(0)

    log("✗ Initial run FAILED — entering debug loop")
    log(f"  output tail:\n{output[-1500:]}")

    final_ok, final_output = repair_integration_failure(
        service=frontend_service,
        workspace=frontend_ws,
        failure_output=output,
        integration_test_rel=target_rel,
        rerun_tests=_rerun_playwright,
        on_status=log,
        compose_path=compose_path,
        problem_statement=problem_statement,
        escalation=escalation,
    )

    print("\n" + "="*60)
    if final_ok:
        print("  ✓ Frontend FIXED after debug loop")
    else:
        print("  ✗ Frontend STILL FAILING — see output above")
        print(f"  Last output tail:\n{final_output[-1500:]}")
    print("="*60)

    if not final_ok:
        sys.exit(2)


if __name__ == "__main__":
    main()
