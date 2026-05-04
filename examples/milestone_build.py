"""
Milestone-driven build — run one milestone at a time with human verification.

Usage:
    # Step 1: Plan + execute milestone 1 (greenfield)
    cd ~/bizniz && set -a && source .env && set +a \
      && PYTHONPATH=. .venv/bin/python -u examples/milestone_build.py

    # Step 2: After verifying M1, run milestone 2 (evolve)
    ... examples/milestone_build.py --resume --milestone 2

    # Plan only (no execution — just see what the planner produces)
    ... examples/milestone_build.py --plan-only

    # Execute a specific milestone range
    ... examples/milestone_build.py --milestone 1
    ... examples/milestone_build.py --milestone 2
    ... examples/milestone_build.py --milestone 1-3

    # Run integration tests against the current project state
    ... examples/debug_integration.py ~/bizniz_projects/<slug>
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

from bizniz.architect.architect import Architect
from bizniz.architect.types import SystemArchitecture, ArchitectResult
from bizniz.agents.coder.coder import Coder
from bizniz.agents.debugger.quick import QuickDebugger
from bizniz.agents.debugger.agentic import AgenticDebugger
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
from bizniz.environment.docker_jest_environment import DockerJestEnvironment
from bizniz.engineer.engineer import Engineer
from bizniz.integration.http_api_tester import HTTPApiTester
from bizniz.integration.web_ui_tester import WebUITester
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.planner import Planner
from bizniz.planner.types import ProjectPlan, Milestone
from bizniz.tester.tester import Tester
from bizniz.workspace.local_workspace import LocalWorkspace


_start_time = time.time()


def log(msg: str):
    elapsed = time.time() - _start_time
    print(f"  [{elapsed:6.1f}s] {msg}", flush=True)


# ── Problem Statement ──────────────────────────────────────────────────────

PROBLEM_STATEMENT = """\
Build a property management web application for small landlords (1-20 units).

The system must support:

1) TENANT MANAGEMENT
   - Landlord can add properties (address, unit count, description, photos)
   - Landlord can add tenants and assign them to units
   - Each tenant has: name, email, phone, lease start/end dates, monthly rent amount
   - Tenants can log in and see their own lease details and payment history

2) RENT COLLECTION
   - System tracks monthly rent for each tenant
   - Landlord can record manual payments (check, cash) with date and amount
   - Tenants see their payment history and current balance
   - Overdue rent is flagged automatically (grace period: 5 days after the 1st)

3) MAINTENANCE REQUESTS
   - Tenants can submit maintenance requests (title, description, urgency: low/medium/high/emergency)
   - Landlord sees all open requests, can assign status (open/in_progress/completed)
   - Both parties can add comments to a request
   - Email notifications on status changes (use a notification queue, actual sending is mocked)

4) AUTHENTICATION & AUTHORIZATION
   - JWT-based authentication (access + refresh tokens)
   - Two roles: landlord and tenant
   - Landlord sees all properties, tenants, payments, and requests
   - Tenant sees only their own data
   - Registration with email verification (verification is mocked, but the flow must exist)

TECHNICAL REQUIREMENTS:
- Backend: REST API with PostgreSQL database (not in-memory — use real migrations)
- Frontend: React with proper routing and state management
- All API endpoints must validate input and return proper error codes
- Database schema must use foreign keys and proper constraints
"""

PROJECT_NAME = "Property Manager V1"


# ── Factory helpers (same pattern as auto_architect.py) ────────────────────

def _make_orchestrator(config, workspace, on_status_message=None, suggested_model=None, image_name=None, language="python"):
    sandbox = DockerExecutionEnvironment()
    if language == "typescript":
        test_env = DockerJestEnvironment(
            workspace_root=workspace.root,
            image=image_name or "bizniz-node-runner",
        )
    else:
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
        coder_progression=config.make_autocoder_progression(),
        tester_progression=config.make_autotester_progression(),
        repair_progression=config.make_repair_progression(),
        stall_threshold=config.stall_threshold,
        agentic_debug_threshold=config.agentic_debug_threshold,
        max_iterations=config.max_iterations,
        on_status_message=on_status_message,
        language=language,
        enable_agentic_debug=config.enable_agentic_debug,
        stall_recovery=config.stall_recovery,
    )


def _make_engineer(config, workspace, on_status_message=None, image_name=None, language="python"):
    def orchestrator_factory(suggested_model=None):
        return _make_orchestrator(
            config, workspace,
            on_status_message=on_status_message,
            suggested_model=suggested_model,
            image_name=image_name,
            language=language,
        )

    engineer_client = config.make_engineer_client()

    return Engineer(
        client=engineer_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=orchestrator_factory,
        on_status_message=on_status_message,
        language=language,
        available_models=config.coder_models or config.models,
        debugger_model=config.debugger_model,
        debugger_max_iterations=config.debugger_max_iterations,
    )


def _parse_milestone_range(spec: str) -> tuple[int, int]:
    """Parse '1', '2', or '1-3' into (start, end) 0-based indices."""
    if "-" in spec:
        parts = spec.split("-", 1)
        return int(parts[0]) - 1, int(parts[1]) - 1
    n = int(spec) - 1
    return n, n


def _save_plan(plan: ProjectPlan, plan_path: Path):
    """Save plan to JSON for resume."""
    data = plan.model_dump()
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(data, indent=2, default=str))


def _load_plan(plan_path: Path) -> ProjectPlan:
    """Load plan from JSON."""
    data = json.loads(plan_path.read_text())
    return ProjectPlan(**data)


def main():
    parser = argparse.ArgumentParser(description="Milestone-driven build")
    parser.add_argument("--plan-only", action="store_true",
                        help="Plan milestones but don't execute")
    parser.add_argument("--milestone", type=str, default="1",
                        help="Milestone to execute: '1', '2', or '1-3' (default: 1)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from saved plan (skip re-planning)")
    parser.add_argument("--continue-on-failure", action="store_true",
                        help="Keep going if a milestone fails")
    args = parser.parse_args()

    m_start, m_end = _parse_milestone_range(args.milestone)

    print(f"\n{'='*60}", flush=True)
    print(f"  Milestone Build — {PROJECT_NAME}", flush=True)
    print(f"{'='*60}\n", flush=True)

    # Preflight
    config = BiznizConfig.find_and_load()
    log(f"Config: architect={config.architect_model}, engineer={config.engineer_model}, planner={config.planner_model}")

    from bizniz.workspace.naming import slugify
    project_slug = slugify(PROJECT_NAME)
    project_parent = Path.home() / "bizniz_projects"
    project_parent.mkdir(parents=True, exist_ok=True)
    project_root = project_parent / project_slug

    plan_path = project_root / "docs" / "plan.json"

    # ── Step 1: Plan ───────────────────────────────────────────────────
    if args.resume and plan_path.exists():
        log(f"Resuming from saved plan: {plan_path}")
        plan = _load_plan(plan_path)
    else:
        log("Planning milestones...")
        planner_client = config.make_client(model=config.planner_model)
        planner = Planner(
            client=planner_client,
            environment=PythonSandboxExecutionEnvironment(),
            workspace=LocalWorkspace(root=str(project_root)),
            on_status_message=log,
        )
        plan = planner.plan(
            problem_statement=PROBLEM_STATEMENT,
            project_name=PROJECT_NAME,
        )
        _save_plan(plan, plan_path)
        log(f"Plan saved to {plan_path}")

    # Display plan
    print(f"\n{'─'*60}", flush=True)
    print(f"  Plan: {len(plan.milestones)} milestone(s)", flush=True)
    print(f"{'─'*60}", flush=True)
    for m in sorted(plan.milestones, key=lambda x: x.sequence_index):
        marker = " ◀" if m_start <= m.sequence_index <= m_end else ""
        status = f" [{m.status}]" if m.status != "planned" else ""
        print(f"  M{m.sequence_index + 1}: {m.name} ({m.estimated_effort or '?'}){status}{marker}", flush=True)
        for uc in m.use_cases[:3]:
            print(f"      - {uc}", flush=True)
        if len(m.use_cases) > 3:
            print(f"      ... +{len(m.use_cases) - 3} more", flush=True)
    print(flush=True)

    if args.plan_only:
        log("Plan-only mode — not executing.")
        print(f"\n  Plan written to: {plan_path}", flush=True)
        print(f"  Run with: --milestone 1  (to execute M1)", flush=True)
        print(flush=True)
        return

    # ── Step 2: Execute milestone(s) ───────────────────────────────────
    milestones_to_run = [
        m for m in plan.milestones
        if m_start <= m.sequence_index <= m_end
    ]
    if not milestones_to_run:
        log(f"No milestones in range {m_start+1}-{m_end+1}")
        sys.exit(1)

    log(f"Executing milestone(s) {m_start+1} to {m_end+1}...")

    # Build a plan subset for build_with_plan
    run_plan = ProjectPlan(
        project_slug=plan.project_slug,
        problem_statement=plan.problem_statement,
        description=plan.description,
        milestones=milestones_to_run,
    )

    # Architect + factories
    architect_client = config.make_client(model=config.architect_model)
    root_workspace = LocalWorkspace(root=str(project_root))

    def _make_http_api_tester(workspace):
        return HTTPApiTester(
            client=config.make_integration_tester_client(),
            environment=PythonSandboxExecutionEnvironment(),
            workspace=workspace,
            on_status_message=log,
        )

    def _make_integration_debugger(workspace):
        return AgenticDebugger(
            client=config.make_client(model=config.debugger_model),
            workspace=workspace,
            environment=PythonSandboxExecutionEnvironment(),
            on_status_message=log,
        )

    def _make_web_ui_tester(workspace):
        return WebUITester(
            client=config.make_integration_tester_client(),
            environment=PythonSandboxExecutionEnvironment(),
            workspace=workspace,
            on_status_message=log,
        )

    def _make_ux_designer_kwargs():
        """Return kwargs dict for run_ux_review (vision_client + coder_factory)."""
        from bizniz.clients.gemini.gemini_client import GeminiClient
        vision_client = GeminiClient(model_name="gemini-flash")
        def _coder_for_ux(workspace):
            from bizniz.agents.coder.coder import Coder
            return Coder(
                client=config.make_client(),
                environment=DockerExecutionEnvironment(),
                workspace=workspace,
            )
        return {
            "vision_client": vision_client,
            "coder_factory": _coder_for_ux,
        }

    architect = Architect(
        client=architect_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=root_workspace,
        engineer_factory=lambda ws, on_status_message=None, image_name=None, language="python": _make_engineer(
            config, ws, on_status_message=on_status_message, image_name=image_name, language=language,
        ),
        http_api_tester_factory=lambda workspace: _make_http_api_tester(workspace),
        integration_debugger_factory=lambda workspace: _make_integration_debugger(workspace),
        web_ui_tester_factory=lambda workspace: _make_web_ui_tester(workspace),
        ux_designer_factory=_make_ux_designer_kwargs,
        project_parent=str(project_parent),
        on_status_message=log,
    )

    try:
        results = architect.build_with_plan(
            problem_statement=PROBLEM_STATEMENT,
            project_name=PROJECT_NAME,
            plan=run_plan,
            parallel=config.parallel_services,
            max_workers=config.max_service_workers,
            layered=config.layered_generation,
            continue_on_failure=args.continue_on_failure,
        )
    except KeyboardInterrupt:
        log("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        log(f"BUILD FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # ── Results ────────────────────────────────────────────────────────
    print(f"\n{'='*60}", flush=True)
    print(f"  Milestone Results", flush=True)
    print(f"{'='*60}", flush=True)

    for i, result in enumerate(results):
        m = milestones_to_run[i] if i < len(milestones_to_run) else None
        m_label = f"M{m.sequence_index + 1}: {m.name}" if m else f"Result {i}"
        print(f"\n  {m_label}", flush=True)
        print(f"  Services: {len(result.architecture.services)}", flush=True)
        for sr in result.service_results:
            status = "PASS" if sr.success else "FAIL"
            print(f"    {sr.service_name}: {status} ({sr.issues_passed}/{sr.issues_total})", flush=True)

    elapsed = time.time() - _start_time
    print(f"\n  Total elapsed: {elapsed:.0f}s", flush=True)

    # Cost
    try:
        from bizniz.cost import get_tracker
        cost = get_tracker().summary()
        print(f"\n{'='*60}", flush=True)
        print(f"  Cost", flush=True)
        print(f"{'='*60}", flush=True)
        print("  " + cost.format().replace("\n", "\n  "), flush=True)
    except Exception as e:
        print(f"  Cost summary unavailable: {e}", flush=True)

    # Update plan statuses and save
    for m in milestones_to_run:
        m_idx = m.sequence_index
        if m_idx < len(results):
            r = results[m_idx - m_start]
            all_pass = all(sr.success for sr in r.service_results) if r.service_results else False
            m.status = "completed" if all_pass else "failed"
    _save_plan(plan, plan_path)

    all_passed = all(
        sr.success
        for r in results
        for sr in r.service_results
    )
    print(f"\n  Overall: {'PASS' if all_passed else 'FAIL'}", flush=True)
    print(f"\n  Next steps:", flush=True)
    print(f"  1. Verify: docker compose -f {project_root}/infra/development/docker-compose.yml up -d", flush=True)
    print(f"  2. Integration tests: PYTHONPATH=. .venv/bin/python -u examples/debug_integration.py {project_root}", flush=True)
    next_m = m_end + 2  # 1-based
    if next_m <= len(plan.milestones):
        print(f"  3. Next milestone: .venv/bin/python -u examples/milestone_build.py --resume --milestone {next_m}", flush=True)
    print(flush=True)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
