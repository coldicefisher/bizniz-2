"""
UX Designer Harness

Takes an already-built project and runs ONLY the UX review phase
(screenshot → vision evaluation → fix loop). Lets you iterate on the
UX designer prompts/wait logic without re-paying engineering cost.

Usage:
    cd ~/bizniz && set -a && source .env && set +a \\
      && PYTHONPATH=. .venv/bin/python -u examples/debug_ux.py \\
         ~/bizniz_projects/property_manager_v1

Flags:
    --no-fixes              Skip the coder-driven fix step (eval only)
    --max-fix-iterations N  Override fix iterations (default 2)
    --acceptable-score N    Override skip-when-good threshold (default 6)
    --keep-up               Don't tear the stack down after the run
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ["PYTHONUNBUFFERED"] = "1"

from dotenv import load_dotenv
load_dotenv()

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.workspace.local_workspace import LocalWorkspace


_start_time = time.time()


def log(msg: str):
    elapsed = time.time() - _start_time
    print(f"  [{elapsed:6.1f}s] {msg}", flush=True)


def reconstruct_architecture(project_root: Path) -> SystemArchitecture:
    """Discover services from the project directory.

    Looks for `frontend/` (react/vite) and `backend/` (fastapi). Reads the
    docker-compose to confirm port mappings. UX review only needs frontends,
    but we include the backend so the stack-up step brings everything.
    """
    services = []
    if (project_root / "backend").is_dir():
        services.append(ServiceDefinition(
            name="backend",
            service_type="backend",
            framework="fastapi",
            language="python",
            description="Backend API",
            workspace_name="backend",
            port=8000,
            depends_on=[],
            requirements=[],
            skeleton="fastapi",
        ))
    if (project_root / "frontend").is_dir():
        services.append(ServiceDefinition(
            name="frontend",
            service_type="frontend",
            framework="react",
            language="typescript",
            description="React frontend",
            workspace_name="frontend",
            port=5173,
            depends_on=["backend"],
            requirements=[],
            skeleton="react",
        ))

    if not any(s.service_type == "frontend" for s in services):
        print("  ERROR: no frontend service found — UX review is frontend-only", flush=True)
        sys.exit(1)

    slug = project_root.name
    return SystemArchitecture(
        project_name=slug,
        project_slug=slug,
        services=services,
        description=f"UX review for {slug}",
    )


def find_problem_statement(project_root: Path) -> str:
    """Recover the problem statement from the project, or fall back."""
    candidates = [
        project_root / "docs" / "problem_statement.txt",
        project_root / "docs" / "PROBLEM.md",
        project_root / "PROBLEM.md",
    ]
    for c in candidates:
        if c.exists():
            return c.read_text()

    # Fall back to the e2e fixture for property_manager
    repo_root = Path(__file__).resolve().parent.parent
    fixture = repo_root / "tests" / "e2e" / "property_manager" / "problem_statement.txt"
    if fixture.exists() and "property_manager" in project_root.name:
        return fixture.read_text()

    return f"Web application for {project_root.name}"


def main():
    parser = argparse.ArgumentParser(description="UX Designer harness")
    parser.add_argument("project_root", type=Path, help="Path to built project")
    parser.add_argument("--no-fixes", action="store_true",
                        help="Eval only — don't dispatch Coder to apply fixes")
    parser.add_argument("--max-fix-iterations", type=int, default=2,
                        help="Max evaluate→fix→re-evaluate cycles (default 2)")
    parser.add_argument("--acceptable-score", type=int, default=6,
                        help="Skip fixes if score >= this and no major issues (default 6)")
    parser.add_argument("--keep-up", action="store_true",
                        help="Leave the stack running after the review")
    parser.add_argument("--milestone-scope", type=str, default="",
                        help="Milestone problem slice (defaults to full statement)")
    args = parser.parse_args()

    project_root = args.project_root.expanduser().resolve()
    if not project_root.is_dir():
        print(f"  ERROR: {project_root} is not a directory", flush=True)
        sys.exit(1)

    compose_path = str(project_root / "infra" / "development" / "docker-compose.yml")
    if not Path(compose_path).exists():
        print(f"  ERROR: {compose_path} not found", flush=True)
        sys.exit(1)

    print(f"\n{'='*60}", flush=True)
    print(f"  UX Designer Harness", flush=True)
    print(f"{'='*60}\n", flush=True)
    log(f"Project: {project_root}")

    config = BiznizConfig.find_and_load()

    architecture = reconstruct_architecture(project_root)
    frontends = [s for s in architecture.services if s.service_type == "frontend"]
    log(f"Frontends to review: {', '.join(s.name for s in frontends)}")

    problem_statement = find_problem_statement(project_root)
    log(f"Problem statement: {len(problem_statement)} chars")

    service_workspaces = {}
    for svc in architecture.services:
        ws_path = project_root / svc.workspace_name
        if ws_path.is_dir():
            service_workspaces[svc.name] = LocalWorkspace(root=str(ws_path), create=False)

    # Stack up
    log("Bringing stack up for screenshots...")
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", compose_path, "up", "-d"],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            log(f"docker compose up failed:\n{proc.stderr[-500:]}")
            sys.exit(1)
    except subprocess.TimeoutExpired:
        log("docker compose up timed out after 180s")
        sys.exit(1)

    # Wait for frontend(s) to be reachable on the host
    from bizniz.integration.runner import _wait_http_ok
    for fe in frontends:
        url = f"http://localhost:{fe.port}/"
        log(f"Waiting for {url}...")
        ok = _wait_http_ok(url, deadline_s=120)
        if not ok:
            log(f"WARNING: {url} did not respond — review may fail to capture screenshots")

    # Vision/script client — ClaudeCliClient since ProUXDesigner
    # uses claude --print + Read for both code review and vision eval.
    from bizniz.clients.claude_cli.claude_cli_client import ClaudeCliClient
    vision_client = ClaudeCliClient(model_name="claude-cli")

    if args.no_fixes:
        coder_factory_for_designer = None
        log("Mode: eval-only (--no-fixes)")
    else:
        # ClaudeCliCoder bound per-frontend. ProUXDesigner calls this
        # only during the home-page fix step; the global-design step
        # uses its own claude --print invocation with Edit/Write
        # directly.
        def make_coder_factory(frontend_service):
            def factory(workspace):
                from bizniz.coder.claude_cli_coder import ClaudeCliCoder
                return ClaudeCliCoder(
                    workspace=workspace,
                    compose_path=compose_path,
                    target_service=frontend_service.name,
                    workspace_name=frontend_service.workspace_name,
                    on_status=log,
                    runner="vitest",
                    model_name="claude-cli",
                )
            return factory

    log("Starting UX review (ProUXDesigner)...")
    print(f"\n{'─'*60}", flush=True)

    from bizniz.ux_designer.pro_ux_designer import ProUXDesigner

    try:
        results = []
        for frontend in frontends:
            ws = service_workspaces.get(frontend.name)
            if ws is None:
                log(f"Skipping '{frontend.name}': no workspace")
                continue
            coder_factory_bound = (
                None if args.no_fixes else make_coder_factory(frontend)
            )
            designer = ProUXDesigner(
                vision_client=vision_client,
                coder_factory=coder_factory_bound,
                on_status=log,
                max_home_iterations=args.max_fix_iterations,
                acceptable_score=args.acceptable_score,
            )
            # Pass AUTH_CONTRACT.md if present so the Playwright
            # screenshot generator knows to authenticate before
            # visiting protected routes. Without this the script
            # captures /login over and over for every protected
            # route (dashboard, admin, recipes/*).
            auth_contract_path = project_root / "AUTH_CONTRACT.md"
            auth_contract = (
                auth_contract_path.read_text()
                if auth_contract_path.exists() else None
            )
            r = designer.review_frontend(
                service=frontend,
                workspace=ws,
                compose_path=compose_path,
                problem_statement=problem_statement,
                milestone_scope=args.milestone_scope,
                auth_contract=auth_contract,
            )
            results.append(r)
    except KeyboardInterrupt:
        log("Interrupted by user")
        if not args.keep_up:
            subprocess.run(["docker", "compose", "-f", compose_path, "down"],
                           capture_output=True, text=True, timeout=60)
        sys.exit(130)
    except Exception as e:
        log(f"UX REVIEW FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        if not args.keep_up:
            subprocess.run(["docker", "compose", "-f", compose_path, "down"],
                           capture_output=True, text=True, timeout=60)
        sys.exit(1)

    # Results
    print(f"\n{'='*60}", flush=True)
    print(f"  UX Review Results (ProUXDesigner)", flush=True)
    print(f"{'='*60}", flush=True)
    for r in results:
        service = r.get("service", "?")
        plan = r.get("design_plan") or {}
        gf = r.get("global_fix_result") or {}
        views = r.get("views") or []
        initial = r.get("initial_score")
        final = r.get("final_score")
        stopped = r.get("stopped_reason") or ""
        print(
            f"  {service}: app_type={plan.get('app_type', '?')}  "
            f"home score {initial}→{final}/10  "
            f"views={len(views)}  stopped={stopped}",
            flush=True,
        )
        print(
            f"    global: status={gf.get('status', '?')}  "
            f"tailwind_wired={gf.get('tailwind_wired', '?')}  "
            f"files_written={len(gf.get('files_written', []))}",
            flush=True,
        )
        if plan.get("summary"):
            print(f"    plan: {plan.get('summary')[:180]}", flush=True)
        for view_r in views:
            route = view_r.get("route", "?")
            vtype = view_r.get("view_type", "?")
            v_initial = view_r.get("initial_score")
            v_final = view_r.get("final_score")
            v_stop = view_r.get("stopped_reason") or "-"
            print(
                f"    {route} ({vtype}): {v_initial}→{v_final}/10  "
                f"iters={len(view_r.get('iterations') or [])}  stop={v_stop}",
                flush=True,
            )
            for iter_r in view_r.get("iterations") or []:
                ev = iter_r.get("evaluation") or {}
                n = iter_r.get("iteration")
                score = ev.get("overall_score")
                issues = (ev.get("issues") or [])
                print(
                    f"      iter{n}: score={score}/10 issues={len(issues)}",
                    flush=True,
                )
                for issue in issues[:2]:
                    sev = issue.get("severity", "?")
                    cat = issue.get("category", "?")
                    desc = issue.get("description", "")[:100]
                    print(f"        [{sev}/{cat}] {desc}", flush=True)

    elapsed = time.time() - _start_time
    print(f"\n  Elapsed: {elapsed:.0f}s", flush=True)

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

    # Surface where the screenshots landed
    for fe in frontends:
        sd = project_root / fe.workspace_name / "screenshots"
        if sd.exists():
            shots = sorted(sd.glob("*.png"))
            print(f"\n  Screenshots ({fe.name}): {sd}", flush=True)
            for s in shots:
                print(f"    {s.name}  ({s.stat().st_size // 1024} KB)", flush=True)

    if not args.keep_up:
        log("Tearing down stack...")
        subprocess.run(["docker", "compose", "-f", compose_path, "down"],
                       capture_output=True, text=True, timeout=120)

    sys.exit(0)


if __name__ == "__main__":
    main()
