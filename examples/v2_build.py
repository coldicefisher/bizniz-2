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
from bizniz.driver.gates import GatePolicy
from bizniz.driver.integration_phase import IntegrationPhase
from bizniz.driver.milestone_loop import MilestoneLoop
from bizniz.driver.pipeline import V2Pipeline
from bizniz.driver.state import RunState
from bizniz.engineer.agent import Engineer
from bizniz.integration.http_api_tester import HTTPApiTester
from bizniz.integration.web_ui_tester import WebUITester
from bizniz.planner.planner import Planner
from bizniz.provisioner.provisioner import Provisioner
from bizniz.quality_engineer.agent import QualityEngineer
from bizniz.workspace.local_workspace import LocalWorkspace


def _client_for(model: str):
    """Build a client for ``model`` using the prefix-routing convention."""
    if model.startswith("claude"):
        from bizniz.clients.claude.claude_client import ClaudeClient
        return ClaudeClient(model=model)
    if model.startswith("gemini"):
        from bizniz.clients.gemini.gemini_client import GeminiClient
        return GeminiClient(model=model)
    from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient
    return ChatGPTClient(model=model)


def _on_status(prefix: str = ""):
    """Standard stdout status callback with optional prefix."""
    def cb(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {prefix}{msg}" if prefix else f"[{ts}] {msg}"
        print(line, flush=True)
    return cb


def _resolve_runs_root(project_slug: str) -> Path:
    base = Path(os.environ.get("BIZNIZ_PROJECTS_ROOT") or
                str(Path.home() / "bizniz_projects"))
    return base / project_slug / "docs" / "runs"


def _new_job_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _build_pipeline(args, on_status) -> V2Pipeline:
    config = BiznizConfig.load()

    planner_client = _client_for(config.planner_model or config.architect_model)
    architect_client = _client_for(config.architect_model)
    engineer_client = _client_for(config.engineer_model)
    qe_client = _client_for(config.architect_model)
    cr_client = _client_for(config.architect_model)
    tester_client = _client_for(getattr(config, "tester_model", config.architect_model))

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
            client=_client_for(tier_model),
            workspace=primary_workspace,
            compose_path=compose_path,
            target_service="backend",
            on_status=on_status,
        )

    def http_tester_factory(workspace):
        return HTTPApiTester(client=tester_client, workspace=workspace)

    def web_tester_factory(workspace):
        return WebUITester(client=tester_client, workspace=workspace)

    integration_phase = IntegrationPhase(
        http_tester_factory=http_tester_factory,
        web_tester_factory=web_tester_factory,
        debugger_factory=None,  # AgenticDebugger wiring deferred to next pass
        debugger_max_iterations=3,
        on_status=on_status,
    )

    gate_mode = "auto" if args.auto else ("interactive" if args.interactive else "strict")
    gates = GatePolicy(mode=gate_mode, on_status=on_status)

    runs_root = _resolve_runs_root(project_slug)
    job_id = args.resume_job_id or _new_job_id()
    run_state = RunState(runs_root / job_id)

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
        on_status=on_status,
    )

    def auth_agent_factory(architecture):
        from bizniz.workspace.local_workspace import LocalWorkspace
        fa_orch = FusionAuthOrchestrator()
        return AuthAgent(
            client=_client_for(config.architect_model),
            workspace=primary_workspace,
            fa_orchestrator=fa_orch,
            on_status=on_status,
        )

    provisioner = Provisioner(
        project_parent=Path(os.environ.get("BIZNIZ_PROJECTS_ROOT") or
                             str(Path.home() / "bizniz_projects")),
        on_status=on_status,
    )

    def provision_callable(architecture, project_name):
        return provisioner.provision(architecture, project_name)

    return V2Pipeline(
        planner=planner,
        architect=architect,
        auth_agent_factory=auth_agent_factory,
        provision_callable=provision_callable,
        milestone_loop=milestone_loop,
        gates=gates,
        run_state=run_state,
        project_name=project_slug,
        compose_path_for_arch=lambda _arch: compose_path,
        on_status=on_status,
    )


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
    args = p.parse_args()

    if not args.problem and not args.resume:
        p.error("either provide a problem statement or pass --resume")

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
    )

    if result.halted_at:
        on_status(f"HALTED at {result.halted_at}: {result.halt_reason}")
        sys.exit(1)
    on_status(f"DONE — {len(result.milestones_completed)} milestone(s) completed")
    sys.exit(0)


if __name__ == "__main__":
    main()
