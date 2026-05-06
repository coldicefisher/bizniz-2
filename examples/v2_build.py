"""v2-build — top-level CLI for the v2 pipeline driver.

Usage:
  v2-build "<problem statement>" --project <slug>
  v2-build "<problem statement>" --project <slug> --plan-only
  v2-build "<problem statement>" --project <slug> --milestone 2
  v2-build --project <slug> --resume
  v2-build "<problem statement>" --project <slug> --auto

Reads ``bizniz.yaml`` from the current directory (or parents) for model
config. Writes per-run state to ``~/bizniz_projects/<slug>/docs/runs/<job_id>/``.

This is a thin shell: argparse + wiring. The driver lives in
``bizniz/driver/`` and is fully testable without going through here.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Ensure repo root is on PYTHONPATH when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bizniz.architect.architect import Architect
from bizniz.auth_agent.agent import AuthAgent
from bizniz.auth_orchestrators.fusionauth_orchestrator import FusionAuthOrchestrator
from bizniz.code_reviewer.agent import CodeReviewer
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.cost import get_tracker
from bizniz.driver.gates import GatePolicy
from bizniz.driver.integration_phase import IntegrationPhase
from bizniz.driver.milestone_loop import MilestoneLoop
from bizniz.driver.pipeline import V2Pipeline
from bizniz.driver.state import RunState
from bizniz.agents.debugger.agentic import AgenticDebugger
from bizniz.engineer.agent import Engineer
from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
from bizniz.integration.http_api_tester import HTTPApiTester
from bizniz.integration.web_ui_tester import WebUITester
from bizniz.integration.worker_tester import WorkerTester
from bizniz.planner.planner import Planner
from bizniz.provisioner.provisioner import Provisioner
from bizniz.quality_engineer.agent import QualityEngineer
from bizniz.workspace.local_workspace import LocalWorkspace


def _client_for(model: str, agent_label: str = "unknown"):
    """Build a client for ``model`` using the prefix-routing convention.

    ``agent_label`` is stamped onto the client as ``_caller_agent`` so
    the cost tracker can group records per agent (clients auto-record
    on every call and read this attribute).
    """
    if model.startswith("claude"):
        from bizniz.clients.claude.claude_client import ClaudeClient
        client = ClaudeClient(model_name=model)
    elif model.startswith("gemini"):
        from bizniz.clients.gemini.gemini_client import GeminiClient
        client = GeminiClient(model_name=model)
    else:
        from bizniz.clients.openai.chatgpt_client import ChatGPTClient
        client = ChatGPTClient(model_name=model)
    client._caller_agent = agent_label
    return client


def _on_status(prefix: str = ""):
    """Standard stdout status callback with optional prefix."""
    def cb(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {prefix}{msg}" if prefix else f"[{ts}] {msg}"
        print(line, flush=True)
    return cb


def _resolve_fa_endpoint(project_root: Path) -> tuple[str, str]:
    """Read FUSIONAUTH_HOST_URL + FUSIONAUTH_API_KEY from the project's
    generated infra/.env. Falls back to env vars if the file isn't there
    (e.g., in tests). The provisioner emits these every run.
    """
    env_path = project_root / "infra" / "development" / ".env"
    url = ""
    key = ""
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("FUSIONAUTH_HOST_URL="):
                url = line.split("=", 1)[1].strip()
            elif line.startswith("FUSIONAUTH_API_KEY="):
                key = line.split("=", 1)[1].strip()
    return (
        url or os.environ.get("FUSIONAUTH_HOST_URL", "http://localhost:9011"),
        key or os.environ.get("FUSIONAUTH_API_KEY", ""),
    )


def _seed_fa_env_from_project(project_root: Path) -> None:
    """Export FUSIONAUTH_* vars from the project's infra/.env into the
    process env so pipeline-internal helpers (``_resolve_fa_tenant_id``)
    can query the running FA without explicit args.
    """
    env_path = project_root / "infra" / "development" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        if line.startswith("FUSIONAUTH_") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip())


def _resolve_runs_root(project_slug: str) -> Path:
    base = Path(os.environ.get("BIZNIZ_PROJECTS_ROOT") or
                str(Path.home() / "bizniz_projects"))
    return base / project_slug / "docs" / "runs"


def _new_job_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _build_pipeline(args, on_status) -> V2Pipeline:
    config = BiznizConfig.find_and_load()

    planner_client = _client_for(config.planner_model or config.architect_model, "planner")
    architect_client = _client_for(config.architect_model, "architect")
    engineer_client = _client_for(config.engineer_model, "engineer")
    qe_client = _client_for(config.architect_model, "quality_engineer")
    cr_client = _client_for(config.architect_model, "code_reviewer")
    tester_client = _client_for(getattr(config, "tester_model", config.architect_model), "integration_tester")
    auth_client = _client_for(config.architect_model, "auth_agent")

    # Repair tier escalation: read from config if present, else default to
    # [engineer_model, engineer_model, gemini-pro].
    repair_tiers = list(getattr(config, "repair_escalation", []) or [
        config.engineer_model,
        config.engineer_model,
        "gemini-pro",
    ])

    planner = Planner(client=planner_client, on_status=on_status)
    architect = Architect(client=architect_client, on_status=on_status)
    qe = QualityEngineer(client=qe_client, on_status=on_status)
    cr = CodeReviewer(client=cr_client, on_status=on_status)

    project_slug = args.project
    project_root = Path(os.environ.get("BIZNIZ_PROJECTS_ROOT") or
                        str(Path.home() / "bizniz_projects")) / project_slug
    compose_path = str(project_root / "infra" / "development" / "docker-compose.yml")

    primary_workspace = LocalWorkspace(root=project_root)

    # Seed FUSIONAUTH_* env vars so pipeline FA-ID resolvers can query
    # the running FA. No-op if the project hasn't been provisioned yet.
    _seed_fa_env_from_project(project_root)

    def workspace_for_service(name: str) -> LocalWorkspace:
        # Service workspaces live at <project>/<workspace_name>/
        return LocalWorkspace(root=project_root / name)

    engineer = Engineer(
        client=engineer_client,
        workspace=primary_workspace,
        compose_path=compose_path,
        target_service="backend",  # service-specific tools default; loop overrides per-service
        on_status=on_status,
    )

    def repair_engineer_factory(iteration: int) -> Engineer:
        tier_model = repair_tiers[min(iteration, len(repair_tiers) - 1)]
        return Engineer(
            client=_client_for(tier_model, f"engineer_repair_t{iteration}"),
            workspace=primary_workspace,
            compose_path=compose_path,
            target_service="backend",
            on_status=on_status,
        )

    def http_tester_factory(workspace):
        return HTTPApiTester(client=tester_client, workspace=workspace)

    def web_tester_factory(workspace):
        return WebUITester(client=tester_client, workspace=workspace)

    def worker_tester_factory(workspace):
        return WorkerTester(client=tester_client, workspace=workspace)

    debugger_client = _client_for(
        getattr(config, "debugger_model", config.architect_model), "debugger",
    )

    def debugger_factory(workspace, service):
        """Build a fresh AgenticDebugger bound to ``service``'s container.

        The pytest environment uses the service's built image so imports
        of the service's runtime deps resolve at debugger-call time.
        Falls back to a generic image if the service has no image_name
        recorded yet (early M1 runs before image stamping).
        """
        image = getattr(service, "image_name", None) or "python:3.12-slim"
        env = DockerPytestEnvironment(
            workspace_root=workspace.root if hasattr(workspace, "root") else project_root,
            image=image,
        )
        return AgenticDebugger(
            client=debugger_client,
            workspace=workspace,
            environment=env,
            tool_iterations=15,
            timeout_seconds=600,
            compose_path=compose_path,
            service_name=service.name,
            on_status_message=on_status,
        )

    integration_phase = IntegrationPhase(
        http_tester_factory=http_tester_factory,
        web_tester_factory=web_tester_factory,
        worker_tester_factory=worker_tester_factory,
        debugger_factory=debugger_factory,
        debugger_max_iterations=3,
        problem_statement=args.problem or "",
        on_status=on_status,
    )

    gate_mode = "auto" if args.auto else ("interactive" if args.interactive else "strict")
    gates = GatePolicy(mode=gate_mode, on_status=on_status)

    runs_root = _resolve_runs_root(project_slug)
    job_id = args.resume_job_id or _new_job_id()
    run_state = RunState(runs_root / job_id)

    # Cost tracker: start a job so every recorded call carries the
    # job_id; phase + milestone tags are set per-call by V2Pipeline +
    # MilestoneLoop. The run_status's job_id is reused so the cost log
    # and run state agree.
    cost_tracker = get_tracker()
    if cost_tracker.current_job_id is None:
        cost_tracker.start_job(
            project_slug=project_slug,
            problem_statement=args.problem or "",
            metadata={"run_state_job_id": job_id, "gate_mode": gate_mode},
        )

    milestone_loop = MilestoneLoop(
        engineer=engineer,
        quality_engineer=qe,
        code_reviewer=cr,
        integration_phase=integration_phase,
        gates=gates,
        workspace_for_service=workspace_for_service,
        primary_workspace=primary_workspace,
        compose_path=compose_path,
        project_root=project_root,
        repair_budget=len(repair_tiers),
        repair_engineer_factory=repair_engineer_factory,
        cost_tracker=cost_tracker,
        on_status=on_status,
    )

    def auth_agent_factory(architecture):
        fa_url, fa_key = _resolve_fa_endpoint(project_root)
        fa_orch = FusionAuthOrchestrator(
            base_url=fa_url,
            api_key=fa_key,
            on_status=on_status,
        )
        return AuthAgent(
            client=auth_client,
            workspace=primary_workspace,
            fa_orchestrator=fa_orch,
            on_status=on_status,
        )

    provisioner = Provisioner(
        project_parent=Path(os.environ.get("BIZNIZ_PROJECTS_ROOT") or
                             str(Path.home() / "bizniz_projects")),
        on_status_message=on_status,
    )

    def provision_callable(architecture, project_name):
        return provisioner.provision(architecture, project_name)

    pipeline = V2Pipeline(
        planner=planner,
        architect=architect,
        auth_agent_factory=auth_agent_factory,
        provision_callable=provision_callable,
        milestone_loop=milestone_loop,
        gates=gates,
        run_state=run_state,
        project_name=project_slug,
        compose_path_for_arch=lambda _arch: compose_path,
        cost_tracker=cost_tracker,
        on_status=on_status,
    )
    # Attach cost tracker + run_state to the pipeline-build closure so
    # main() can write the cost report on exit.
    pipeline._build_runs_dir = run_state.root
    pipeline._build_cost_tracker = cost_tracker
    return pipeline


def main():
    p = argparse.ArgumentParser(prog="v2-build", description="v2 pipeline driver")
    p.add_argument("problem", nargs="?", help="Natural-language problem statement")
    p.add_argument("--project", required=True, help="Project slug (e.g. pet_groomer)")
    p.add_argument("--plan-only", action="store_true", help="Run only the Planner, then exit")
    p.add_argument("--milestone", type=int, default=None,
                   help="Run through milestone N (1-indexed, inclusive); default = all")
    p.add_argument("--resume", action="store_true",
                   help="Resume from the most recent job_id in this project's runs dir")
    p.add_argument("--resume-job-id", default=None,
                   help="Resume from a specific job_id (defaults to most recent)")
    p.add_argument("--auto", action="store_true",
                   help="Push through soft gates (warn + continue)")
    p.add_argument("--interactive", action="store_true",
                   help="Halt at every gate for human review")
    p.add_argument(
        "--phase",
        default=None,
        help=(
            "Run ONE phase only and exit. "
            "Top phases: plan|architect|provision|auth. "
            "Sub phases (require --milestone): "
            "enrich|implement|review_initial|review_final|"
            "repair_iter_0|repair_iter_1|repair_iter_2|"
            "integration_api|integration_web. "
            "Aliases: review→review_initial, repair→repair_iter_0."
        ),
    )
    args = p.parse_args()

    if not args.problem and not args.resume and not args.phase:
        p.error("provide a problem statement, --resume, or --phase")

    if args.resume and args.resume_job_id is None:
        runs_root = _resolve_runs_root(args.project)
        if not runs_root.exists():
            p.error(f"no prior runs at {runs_root}; cannot --resume")
        # Pick the most recent.
        candidates = sorted(
            (d for d in runs_root.iterdir() if d.is_dir()),
            key=lambda d: d.name, reverse=True,
        )
        if not candidates:
            p.error(f"no run directories in {runs_root}")
        args.resume_job_id = candidates[0].name

    on_status = _on_status()
    pipeline = _build_pipeline(args, on_status)

    on_status(f"v2-build: project={args.project} job={args.resume_job_id or 'new'} "
              f"mode={'auto' if args.auto else ('interactive' if args.interactive else 'strict')}")

    result = pipeline.run(
        problem_statement=args.problem or "",
        plan_only=args.plan_only,
        target_milestone=args.milestone,
        target_phase=args.phase,
    )

    # Finish the cost tracker job + write a summary to docs/runs/<job>/cost.md.
    tracker = getattr(pipeline, "_build_cost_tracker", None)
    runs_dir = getattr(pipeline, "_build_runs_dir", None)
    if tracker is not None:
        try:
            tracker.finish_job(
                status="halted" if result.halted_at else "succeeded",
            )
        except Exception:
            pass
        if runs_dir is not None:
            try:
                summary = tracker.summary()
                cost_md = runs_dir / "cost.md"
                cost_md.write_text(str(summary))
                on_status(f"Cost report written to {cost_md}")
            except Exception as e:
                on_status(f"Cost report write failed: {type(e).__name__}: {e}")

    if result.halted_at:
        on_status(f"HALTED at {result.halted_at}: {result.halt_reason}")
        sys.exit(1)
    on_status(f"DONE — {len(result.milestones_completed)} milestone(s) completed")
    sys.exit(0)


if __name__ == "__main__":
    main()
