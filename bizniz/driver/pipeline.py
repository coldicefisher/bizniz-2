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

# Friendly --phase aliases for the CLI; expand to canonical SubPhase values.
PHASE_ALIASES: dict = {
    "review": SubPhase.REVIEW_INITIAL,
    "review_final": SubPhase.REVIEW_FINAL,
    "repair": SubPhase.REPAIR_ITER_0,
}
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
        cost_tracker=None,
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
        self._cost_tracker = cost_tracker
        self._on_status = on_status

    def _tag_top(self, phase: TopPhase) -> None:
        if self._cost_tracker is None:
            return
        try:
            # Top phases are not milestone-scoped — clear milestone tag
            # so records aren't accidentally grouped under a prior M.
            self._cost_tracker.set_milestone(None)
            self._cost_tracker.set_phase(phase.value)
        except Exception:
            pass

    # ── Public ─────────────────────────────────────────────────────────

    def run(
        self,
        problem_statement: str,
        plan_only: bool = False,
        target_milestone: Optional[int] = None,
        target_phase: Optional[str] = None,
    ) -> V2PipelineResult:
        """Run the full pipeline (or a slice of it).

        ``plan_only=True`` runs only the Planner + persists the plan,
        then exits.

        ``target_milestone=N`` runs through milestone N (1-indexed)
        inclusive and stops.

        ``target_phase`` (string name of a TopPhase or SubPhase, plus
        friendly aliases ``review``, ``repair``, ``review_final``)
        runs ONLY that phase:
          - top phases (plan/architect/provision/auth) run independently
          - sub phases require ``target_milestone`` to identify which
            milestone's instance to run
        Single-phase mode loads prerequisite artifacts from disk and
        re-runs the requested phase, even if already marked done.
        """
        try:
            if target_phase is not None:
                return self._run_single_phase(
                    problem_statement, target_phase, target_milestone,
                )
            return self._run_inner(problem_statement, plan_only, target_milestone)
        except GateViolation as gv:
            self._log(f"V2Pipeline halted at gate '{gv.gate_name}': {gv.reason}")
            return V2PipelineResult(
                project_slug=self._architect_project_slug() or "",
                halted_at=gv.gate_name,
                halt_reason=gv.reason,
            )

    def _run_single_phase(
        self,
        problem_statement: str,
        target_phase: str,
        target_milestone: Optional[int],
    ) -> V2PipelineResult:
        """Dispatch ``target_phase`` to the right top/sub-phase runner."""
        normalized = (target_phase or "").lower()

        # Top phase?
        for tp in TopPhase:
            if normalized == tp.value:
                return self._run_only_top_phase(tp, problem_statement)

        # Sub phase (with alias resolution)?
        sub: Optional[SubPhase] = PHASE_ALIASES.get(normalized)
        if sub is None:
            try:
                sub = SubPhase(normalized)
            except ValueError:
                self._gates.hard(
                    "invalid_phase",
                    f"unknown --phase '{target_phase}'. Valid: "
                    + ", ".join(
                        [tp.value for tp in TopPhase]
                        + [sp.value for sp in SubPhase if sp != SubPhase.DONE]
                        + sorted(PHASE_ALIASES.keys())
                    ),
                )

        if target_milestone is None:
            self._gates.hard(
                "missing_milestone",
                f"--phase {normalized} requires --milestone N",
            )
        return self._run_only_sub_phase(sub, target_milestone, problem_statement)

    def _run_only_top_phase(
        self, phase: TopPhase, problem_statement: str,
    ) -> V2PipelineResult:
        """Run a single top phase and exit. Other top phases must
        already be done if this phase depends on them (architect
        depends on plan, etc.); we don't auto-chain in single-phase mode.
        """
        self._log(f"V2Pipeline: single top-phase mode — running '{phase.value}'")
        if phase == TopPhase.PLAN:
            self._top_plan(problem_statement)
        elif phase == TopPhase.ARCHITECT:
            plan = self._reload_top(TopPhase.PLAN, ProjectPlan, "plan")
            # In --phase architect mode the CLI usually doesn't pass a
            # problem statement; fall back to the one persisted by the
            # plan so the architect doesn't have to infer the project
            # purpose from project_name alone.
            self._top_architect(
                problem_statement or plan.problem_statement, plan,
            )
        elif phase == TopPhase.PROVISION:
            arch = self._reload_top(TopPhase.ARCHITECT, SystemArchitecture, "architecture")
            self._top_provision(arch)
        elif phase == TopPhase.AUTH:
            arch = self._reload_top(TopPhase.ARCHITECT, SystemArchitecture, "architecture")
            plan = self._reload_top(TopPhase.PLAN, ProjectPlan, "plan")
            # AuthAgent talks to live FA — caller must have stack up first.
            # We don't bring it up here so single-phase auth re-runs can
            # be done against an already-running stack without a tear-down/
            # bring-up cycle. If FA isn't reachable, AuthAgent raises +
            # the gate halts.
            self._top_auth(arch, plan)
        return V2PipelineResult(project_slug=self._architect_project_slug() or "")

    def _run_only_sub_phase(
        self,
        phase: SubPhase,
        milestone_index: int,
        problem_statement: str,
    ) -> V2PipelineResult:
        """Run a single sub-phase for milestone ``milestone_index``.

        Loads plan + architecture from disk; assumes provision + auth
        are already done. Compose stack must already be up.
        """
        self._log(
            f"V2Pipeline: single sub-phase mode — M{milestone_index}/{phase.value}"
        )
        plan = self._reload_top(TopPhase.PLAN, ProjectPlan, "plan")
        architecture = self._reload_top(TopPhase.ARCHITECT, SystemArchitecture, "architecture")

        if milestone_index < 1 or milestone_index > len(plan.milestones):
            self._gates.hard(
                "milestone_out_of_range",
                f"milestone {milestone_index} not in plan "
                f"(plan has {len(plan.milestones)} milestone(s))",
            )

        # Build prior_specs by loading earlier milestones' EnrichedSpec from disk.
        prior_specs: List[EnrichedSpec] = []
        for i in range(1, milestone_index):
            ms_state_i = self._state.milestone(i)
            art = ms_state_i.read_artifact(SubPhase.ENRICH)
            if art is not None:
                try:
                    prior_specs.append(EnrichedSpec.model_validate(art))
                except Exception:
                    pass

        # Auth contract from disk (best effort).
        auth_contract = None
        try:
            import json
            auth_art = json.loads((self._state.root / f"{TopPhase.AUTH.value}.json").read_text())
            auth_contract = auth_art.get("contract_markdown")
        except Exception:
            pass

        ms_state = self._state.milestone(milestone_index)
        milestone = plan.milestones[milestone_index - 1]
        self._milestone_loop.run(
            milestone=milestone,
            architecture=architecture,
            prior_specs=prior_specs,
            auth_contract=auth_contract,
            state=ms_state,
            only_phase=phase,
        )
        return V2PipelineResult(
            project_slug=architecture.project_slug,
            architecture=architecture,
        )

    def _reload_top(self, phase: TopPhase, cls, label: str):
        """Load a top-phase artifact from disk; halt if missing."""
        art_path = self._state.root / f"{phase.value}.json"
        if not art_path.exists():
            self._gates.hard(
                f"{label}_missing",
                f"single-phase mode needs {label}.json on disk; run --phase {phase.value} first",
            )
        try:
            import json
            return cls.model_validate(json.loads(art_path.read_text()))
        except Exception as e:
            self._gates.hard(
                f"{label}_corrupt",
                f"could not reload {label}.json: {e}",
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

        # Bring the stack up BEFORE auth — AuthAgent talks to the live
        # FusionAuth API to configure roles/users; FA must be reachable.
        # MilestoneLoop's integration phase also assumes the stack is up.
        self._compose_up(compose_path)

        auth_contract = self._top_auth(architecture, plan)

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
        self._tag_top(TopPhase.PLAN)
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
        self._tag_top(TopPhase.ARCHITECT)
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
        self._tag_top(TopPhase.PROVISION)
        result = self._provision(architecture, self._project_name)
        self._state.mark_top_phase(TopPhase.PROVISION, _result_payload(result))
        return result

    def _top_auth(
        self,
        architecture: SystemArchitecture,
        plan: ProjectPlan,
    ) -> Optional[str]:
        """Run AuthAgent.configure and return the AUTH_CONTRACT.md text.

        Skipped when the architecture has no auth service — projects
        without auth (e.g., a static-content site) shouldn't be required
        to deploy FusionAuth. The AUTH phase is still marked done with
        a sentinel artifact so resume sees the same "no auth" verdict.
        """
        if self._state.is_top_phase_done(TopPhase.AUTH):
            self._log("V2Pipeline: AUTH already done — loading contract from disk")
            try:
                import json
                raw = json.loads((self._state.root / f"{TopPhase.AUTH.value}.json").read_text())
                return raw.get("contract_markdown")
            except Exception as e:
                self._gates.hard("auth_reload_failed", f"could not reload auth.json: {e}")

        if not _has_auth_service(architecture):
            self._log("V2Pipeline: no auth service in architecture — skipping AUTH phase")
            self._state.mark_top_phase(TopPhase.AUTH, {
                "skipped": True,
                "reason": "no auth service in architecture",
                "contract_markdown": None,
            })
            return None

        self._tag_top(TopPhase.AUTH)
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


def _has_auth_service(architecture: SystemArchitecture) -> bool:
    """True if the architecture declares any service with type ``auth``."""
    for s in architecture.services:
        if (s.service_type or "").lower() == "auth":
            return True
    return False


def _resolve_fa_app_id() -> str:
    """Resolve FusionAuth primary application UUID.

    Order: env var override (for non-default deployments) → the constant
    the provisioner's FA template hardcodes into kickstart. The
    chicken-and-egg the env-var-only approach implied wasn't real:
    every bizniz project gets the same well-known UUID baked into its
    kickstart YAML at provision time, so we can resolve it without
    reading a generated .env file.
    """
    import os
    if os.environ.get("FUSIONAUTH_APPLICATION_ID"):
        return os.environ["FUSIONAUTH_APPLICATION_ID"]
    try:
        from bizniz.provisioner.templates.fusionauth import FusionAuthTemplate
        return FusionAuthTemplate.APPLICATION_ID
    except Exception:
        return ""


def _resolve_fa_tenant_id() -> str:
    """Resolve FusionAuth tenant UUID.

    Order:
      1. ``FUSIONAUTH_TENANT_ID`` env var (explicit override)
      2. Live query against the FA host (``FUSIONAUTH_HOST_URL`` +
         ``FUSIONAUTH_API_KEY``) — picks the tenant named "Default", or
         the first tenant if no "Default" exists. The CLI seeds these
         env vars from the project's generated ``infra/.env`` before
         the pipeline runs, so this path works in normal flows.
      3. Template constant fallback (``00000000-...``) — kept only as a
         last resort since the comment in fusionauth.py claiming this
         is FA's "built-in default" turned out to be wrong: a
         real-world FA assigns the default tenant a random UUID.
    """
    import os
    if os.environ.get("FUSIONAUTH_TENANT_ID"):
        return os.environ["FUSIONAUTH_TENANT_ID"]
    queried = _query_fa_default_tenant_id(
        os.environ.get("FUSIONAUTH_HOST_URL"),
        os.environ.get("FUSIONAUTH_API_KEY"),
    )
    if queried:
        return queried
    try:
        from bizniz.provisioner.templates.fusionauth import FusionAuthTemplate
        return FusionAuthTemplate.DEFAULT_TENANT_ID
    except Exception:
        return ""


def _query_fa_default_tenant_id(
    base_url: Optional[str], api_key: Optional[str],
) -> Optional[str]:
    """GET /api/tenant against a live FA, return the UUID of the
    "Default" tenant (or the first listed). Returns None on any
    failure — caller falls back to the template constant.
    """
    if not base_url or not api_key:
        return None
    import json as _json
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/tenant",
            headers={"Authorization": api_key},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read())
        tenants = data.get("tenants") or []
        for t in tenants:
            if (t.get("name") or "").lower() == "default":
                return t.get("id")
        if tenants:
            return tenants[0].get("id")
    except Exception:
        return None
    return None
