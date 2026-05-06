"""V2Pipeline — top-level orchestration.

Runs Planner → Architect → Provisioner → AuthAgent → per-milestone
MilestoneLoop. Resume-aware: reads RunState; skips top phases already
done; passes per-milestone state down to MilestoneLoop for sub-phase
resume.

Construction is "all batteries included" — caller passes pre-built
agents + factories + state directory. The pipeline's job is purely
sequencing + state-keeping. Building the agents (with the right
clients, models, cost trackers) lives in the CLI / examples script.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from pydantic import BaseModel, Field

from bizniz.architect.architect import Architect
from bizniz.architect.types import SystemArchitecture
from bizniz.auth_agent.agent import AuthAgent
from bizniz.auth_agent.types import AuthAgentResult
from bizniz.driver.gates import GatePolicy, GateViolation
from bizniz.driver.milestone_loop import MilestoneLoop, MilestoneOutcome
from bizniz.driver.state import RunState, SubPhase, TopPhase
from bizniz.planner.planner import Planner
from bizniz.planner.types import Milestone, ProjectPlan
from bizniz.quality_engineer.types import EnrichedSpec


class V2PipelineResult(BaseModel):
    project_slug: str
    architecture: Optional[SystemArchitecture] = None
    milestones_completed: List[str] = Field(default_factory=list)
    halted_at: Optional[str] = None
    halt_reason: Optional[str] = None


class V2Pipeline:
    """Top-level v2 pipeline driver.

    Caller supplies all agent instances + factories. The pipeline owns
    only the sequencing + state.
    """

    def __init__(
        self,
        *,
        planner: Planner,
        architect: Architect,
        auth_agent_factory: Callable[..., AuthAgent],
        provision_callable: Callable[..., object],
        milestone_loop: MilestoneLoop,
        gates: GatePolicy,
        run_state: RunState,
        project_name: str,
        compose_path_for_arch: Callable[[SystemArchitecture], str],
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._planner = planner
        self._architect = architect
        self._auth_agent_factory = auth_agent_factory
        self._provision = provision_callable
        self._milestone_loop = milestone_loop
        self._gates = gates
        self._state = run_state
        self._project_name = project_name
        self._compose_path_for_arch = compose_path_for_arch
        self._on_status = on_status

    # ── Public ─────────────────────────────────────────────────────────

    def run(
        self,
        problem_statement: str,
        plan_only: bool = False,
        target_milestone: Optional[int] = None,
    ) -> V2PipelineResult:
        """Run the full pipeline (or up through ``target_milestone``).

        ``plan_only=True`` runs only the Planner + persists the plan,
        then exits. ``target_milestone=N`` runs through milestone N
        (1-indexed) inclusive and stops; default is all milestones.
        """
        try:
            return self._run_inner(problem_statement, plan_only, target_milestone)
        except GateViolation as gv:
            self._log(f"V2Pipeline halted at gate '{gv.gate_name}': {gv.reason}")
            return V2PipelineResult(
                project_slug=self._architect_project_slug() or "",
                halted_at=gv.gate_name,
                halt_reason=gv.reason,
            )

    # ── Internals ──────────────────────────────────────────────────────

    def _run_inner(
        self,
        problem_statement: str,
        plan_only: bool,
        target_milestone: Optional[int],
    ) -> V2PipelineResult:
        plan = self._top_plan(problem_statement)
        if plan_only:
            return V2PipelineResult(
                project_slug=plan.project_slug,
                milestones_completed=[],
            )

        architecture = self._top_architect(problem_statement, plan)
        compose_path = self._compose_path_for_arch(architecture)

        provision_result = self._top_provision(architecture)
        auth_contract = self._top_auth(architecture, plan)

        # Bring the stack up before milestone work — MilestoneLoop's
        # integration phase assumes the stack is up + healthy.
        self._compose_up(compose_path)

        milestones_done: List[str] = []
        prior_specs: List[EnrichedSpec] = []
        last_milestone = target_milestone or len(plan.milestones)

        for i, milestone in enumerate(plan.milestones, start=1):
            if i > last_milestone:
                break
            ms_state = self._state.milestone(i)
            if ms_state.is_done():
                self._log(f"V2Pipeline: M{i} '{milestone.name}' already DONE — loading spec for prior_specs chain")
                prior_specs.append(self._reload_spec(ms_state))
                milestones_done.append(milestone.name)
                continue

            outcome: MilestoneOutcome = self._milestone_loop.run(
                milestone=milestone,
                architecture=architecture,
                prior_specs=prior_specs,
                auth_contract=auth_contract,
                state=ms_state,
            )
            milestones_done.append(milestone.name)
            if outcome.enriched_spec is not None:
                prior_specs.append(outcome.enriched_spec)

        return V2PipelineResult(
            project_slug=architecture.project_slug,
            architecture=architecture,
            milestones_completed=milestones_done,
        )

    # ── Top phases ─────────────────────────────────────────────────────

    def _top_plan(self, problem_statement: str) -> ProjectPlan:
        if self._state.is_top_phase_done(TopPhase.PLAN):
            self._log("V2Pipeline: PLAN already done — loading from disk")
            art = (self._state.root / f"{TopPhase.PLAN.value}.json")
            try:
                import json
                raw = json.loads(art.read_text())
                return ProjectPlan.model_validate(raw)
            except Exception as e:
                self._gates.hard("plan_reload_failed", f"could not reload plan.json: {e}")
        plan = self._planner.plan(
            problem_statement=problem_statement,
            project_name=self._project_name,
        )
        self._state.mark_top_phase(TopPhase.PLAN, plan)
        return plan

    def _top_architect(
        self, problem_statement: str, plan: ProjectPlan,
    ) -> SystemArchitecture:
        if self._state.is_top_phase_done(TopPhase.ARCHITECT):
            self._log("V2Pipeline: ARCHITECT already done — loading from disk")
            try:
                import json
                raw = json.loads((self._state.root / f"{TopPhase.ARCHITECT.value}.json").read_text())
                return SystemArchitecture.model_validate(raw)
            except Exception as e:
                self._gates.hard("architecture_reload_failed", f"could not reload architecture.json: {e}")
        architecture = self._architect.decompose(
            problem_statement=problem_statement,
            project_name=self._project_name,
        )
        self._state.mark_top_phase(TopPhase.ARCHITECT, architecture)
        return architecture

    def _top_provision(self, architecture: SystemArchitecture):
        if self._state.is_top_phase_done(TopPhase.PROVISION):
            self._log("V2Pipeline: PROVISION already done — skipping (idempotent re-provision is safe but not auto-run)")
            return None
        result = self._provision(architecture, self._project_name)
        self._state.mark_top_phase(TopPhase.PROVISION, _result_payload(result))
        return result

    def _top_auth(
        self,
        architecture: SystemArchitecture,
        plan: ProjectPlan,
    ) -> Optional[str]:
        """Run AuthAgent.configure and return the AUTH_CONTRACT.md text."""
        if self._state.is_top_phase_done(TopPhase.AUTH):
            self._log("V2Pipeline: AUTH already done — loading contract from disk")
            try:
                import json
                raw = json.loads((self._state.root / f"{TopPhase.AUTH.value}.json").read_text())
                return raw.get("contract_markdown")
            except Exception as e:
                self._gates.hard("auth_reload_failed", f"could not reload auth.json: {e}")

        agent = self._auth_agent_factory(architecture=architecture)
        # Caller-injected factory builds the agent already loaded with
        # client + workspace + orchestrator. We just call configure with
        # the milestone-1 problem slice as the seed (auth is set up before
        # any milestone runs; M1's slice typically describes the auth
        # surface area).
        first_slice = (
            plan.milestones[0].problem_slice
            if plan.milestones else
            "(no milestones — configure baseline auth)"
        )
        result: AuthAgentResult = agent.configure(
            problem_slice=first_slice,
            architecture=architecture,
            primary_app_id=_resolve_fa_app_id(),
            tenant_id=_resolve_fa_tenant_id(),
        )
        self._state.mark_top_phase(TopPhase.AUTH, result)
        return result.contract_markdown or None

    # ── Compose helpers ────────────────────────────────────────────────

    def _compose_up(self, compose_path: str) -> None:
        try:
            r = subprocess.run(
                ["docker", "compose", "-f", compose_path, "up", "-d"],
                capture_output=True, text=True, timeout=600,
            )
            if r.returncode != 0:
                self._gates.hard(
                    "compose_up_failed",
                    f"docker compose up failed (rc={r.returncode}): "
                    + (r.stderr or "")[:300],
                )
        except FileNotFoundError:
            self._gates.hard("docker_unavailable", "docker not on PATH")
        except subprocess.TimeoutExpired:
            self._gates.hard("compose_up_timeout", "docker compose up timed out (10m)")

    # ── Misc ────────────────────────────────────────────────────────────

    def _architect_project_slug(self) -> Optional[str]:
        if self._state.is_top_phase_done(TopPhase.ARCHITECT):
            try:
                import json
                raw = json.loads((self._state.root / f"{TopPhase.ARCHITECT.value}.json").read_text())
                return raw.get("project_slug")
            except Exception:
                return None
        return None

    def _reload_spec(self, ms_state) -> EnrichedSpec:
        art = ms_state.read_artifact(SubPhase.ENRICH)
        if art is None:
            self._gates.hard(
                "missing_spec_for_done_milestone",
                f"M{ms_state.milestone_index} marked done but no spec.json on disk",
            )
        return EnrichedSpec.model_validate(art)

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)


# ── Helpers ─────────────────────────────────────────────────────────────


def _result_payload(result) -> dict:
    """Best-effort dict payload for ``RunState.mark_top_phase``."""
    if result is None:
        return {}
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if hasattr(result, "__dict__"):
        return {k: str(v) for k, v in result.__dict__.items() if not k.startswith("_")}
    return {"value": str(result)}


def _resolve_fa_app_id() -> str:
    """Resolve FusionAuth primary application UUID from env."""
    import os
    return os.environ.get("FUSIONAUTH_APPLICATION_ID") or ""


def _resolve_fa_tenant_id() -> str:
    """Resolve FusionAuth tenant UUID from env."""
    import os
    return os.environ.get("FUSIONAUTH_TENANT_ID") or ""
