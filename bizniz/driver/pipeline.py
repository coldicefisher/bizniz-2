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
from bizniz.auth_operator import (
    AuthManifest, FusionAuthOperator, generate_code_examples,
    render_auth_contract,
)
from bizniz.auth_planner import AuthPlanner
from bizniz.driver.gates import GatePolicy, GateViolation
from bizniz.driver.milestone_loop import MilestoneLoop, MilestoneOutcome
from bizniz.driver.project_git import ProjectGit
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
        # v2.6 split-AuthAgent path. When all three are provided,
        # _top_auth uses the planner/operator flow instead of the
        # legacy AuthAgent. Tests + projects without auth still work
        # via the legacy path.
        auth_planner_factory: Optional[Callable[..., AuthPlanner]] = None,
        auth_operator_factory: Optional[Callable[..., FusionAuthOperator]] = None,
        auth_code_examples_client=None,
        project_root: Optional[Path] = None,
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
        self._auth_planner_factory = auth_planner_factory
        self._auth_operator_factory = auth_operator_factory
        self._auth_code_examples_client = auth_code_examples_client
        self._project_root = project_root
        # Per-project git checkpoints (roadmap item 3). Best-effort:
        # if git isn't installed or any op fails, the pipeline keeps
        # running. Initialized on first run inside ``_run_inner`` so
        # the project_root exists by then.
        self._project_git: Optional[ProjectGit] = None

    def _init_project_git(self, architecture: SystemArchitecture) -> None:
        """Lazy-init the per-project git tracker. Idempotent — only
        runs ``git init`` if .git/ doesn't already exist. Safe to call
        on every run."""
        if self._project_git is not None:
            return
        # Resolve project root the same way the rest of v2_build does:
        # if not handed in, derive from BIZNIZ_PROJECTS_ROOT / slug.
        root = self._project_root
        if root is None:
            import os
            base = (
                os.environ.get("BIZNIZ_PROJECTS_ROOT")
                or str(Path.home() / "bizniz_projects")
            )
            root = Path(base) / (
                architecture.project_slug or self._project_name
            )
        self._project_git = ProjectGit(
            project_root=root, on_status=self._on_status,
        )
        self._project_git.init_if_needed()

    def _git_commit(self, message: str, tag: Optional[str] = None) -> None:
        """Best-effort commit helper used at phase boundaries. Never
        raises out to the pipeline — git failures degrade silently to
        the pre-item-3 (no-tracking) behavior."""
        if self._project_git is None:
            return
        try:
            self._project_git.commit_all(message=message, tag=tag)
        except Exception as e:
            if self._on_status:
                try:
                    self._on_status(
                        f"V2Pipeline._git_commit raised "
                        f"{type(e).__name__}: {e} — continuing without"
                    )
                except Exception:
                    pass

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

        # Initialize git tracking for the materialized project (item 3).
        # Idempotent: skips if .git/ already exists. Commits the
        # provisioner's output as the m0 checkpoint so subsequent
        # milestone commits + future refactor reverts have a base
        # state to anchor against.
        self._init_project_git(architecture)
        self._git_commit(
            "Initial provision (architect + provisioner)",
            tag="m0",
        )

        # Bring the stack up BEFORE auth — AuthAgent talks to the live
        # FusionAuth API to configure roles/users; FA must be reachable.
        # MilestoneLoop's integration phase also assumes the stack is up.
        self._compose_up(compose_path)

        auth_contract = self._top_auth(architecture, plan)

        milestones_done: List[str] = []
        prior_specs: List[EnrichedSpec] = []
        last_milestone = target_milestone or len(plan.milestones)
        # MilestoneLoop's REFACTOR phase needs to know whether the
        # current milestone is the last in the plan (treated as a
        # refactor boundary regardless of ``refactor_after``).
        try:
            self._milestone_loop._total_milestones = len(plan.milestones)
        except Exception:
            pass

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
            # Per-milestone checkpoint (item 3). Each milestone DONE
            # gets its own commit + tag so future refactor reverts
            # (item 5) can roll back to any prior milestone cleanly.
            self._git_commit(
                f"M{i}: {milestone.name} DONE",
                tag=f"m{i}-done",
            )

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
        first_slice = (
            plan.milestones[0].problem_slice
            if plan.milestones else
            "(no milestones — configure baseline auth)"
        )

        # v2.6 path: AuthPlanner (LLM, single call) → FusionAuthOperator
        # (deterministic, knows FA quirks) → render contract from manifest
        # → emit per-service contract test files → audit on the manifest.
        # Falls back to legacy AuthAgent when factories aren't wired
        # (existing tests, projects mid-migration).
        if (self._auth_planner_factory is not None
                and self._auth_operator_factory is not None):
            return self._top_auth_v2(
                architecture=architecture,
                problem_slice=first_slice,
            )

        agent = self._auth_agent_factory(architecture=architecture)
        # Caller-injected factory builds the agent already loaded with
        # client + workspace + orchestrator. We just call configure with
        # the milestone-1 problem slice as the seed (auth is set up before
        # any milestone runs; M1's slice typically describes the auth
        # surface area).
        result: AuthAgentResult = agent.configure(
            problem_slice=first_slice,
            architecture=architecture,
            primary_app_id=_resolve_fa_app_id(),
            tenant_id=_resolve_fa_tenant_id(),
        )

        # Gate on AuthAgent crash-before-submit. AuthAgent.configure
        # synthesizes an empty-contract result if the tool loop stalls
        # / errors so the audit can still run, but downstream code
        # generation has nothing useful to consume — halt.
        if not result.contract_markdown:
            self._state.mark_top_phase(TopPhase.AUTH, result)
            self._gates.hard(
                "auth_agent_crashed",
                f"AuthAgent.configure returned without a contract — "
                f"summary: {(result.summary or '(no summary)')[:300]}",
            )

        # Gate on critical audit checks. AuthAgent's tool-loop submission
        # is self-reported, so we trust the deterministic post-loop
        # battery — not the agent's own claim of success. A failure on
        # any of these means downstream code generation will produce
        # auth-broken code (JWT alg mismatch, missing test users → 404
        # at integration time). Halt now rather than burn API spend
        # generating code against a fabricated contract.
        _CRITICAL_AUDIT_CHECKS = {
            "jwt_signing",
            "test_users_in_fa",
            "jwks_reachable",
        }
        critical_failures = [
            c for c in result.audit.checks
            if not c.passed and (
                c.name in _CRITICAL_AUDIT_CHECKS
                or c.name.startswith("token_validation:")
            )
        ]
        if critical_failures:
            detail = "; ".join(
                f"[{c.name}] {c.detail[:160]}" for c in critical_failures
            )
            # Persist the failed result first so resume can see what
            # AuthAgent actually returned (including the contract text
            # the agent tried to ship).
            self._state.mark_top_phase(TopPhase.AUTH, result)
            self._gates.hard(
                "auth_audit_failed",
                f"AuthAgent.configure submitted but audit battery flagged "
                f"{len(critical_failures)} critical failure(s) — refusing to "
                f"proceed with code generation against a contract that "
                f"doesn't match FusionAuth reality. {detail}",
            )

        self._state.mark_top_phase(TopPhase.AUTH, result)
        return result.contract_markdown or None

    # ── v2.6 split-AuthAgent flow ──────────────────────────────────────

    def _top_auth_v2(
        self,
        *,
        architecture: SystemArchitecture,
        problem_slice: str,
    ) -> Optional[str]:
        """AuthPlanner → FusionAuthOperator → render → emit tests → audit.

        All FA mutation is deterministic. The LLM only emits the spec
        (intent). The contract markdown is rendered from the live
        manifest, so it can never claim a user that doesn't exist or
        an algorithm that isn't bound.
        """
        primary_app_id = _resolve_fa_app_id()
        tenant_id = _resolve_fa_tenant_id()

        planner = self._auth_planner_factory(architecture=architecture)
        operator = self._auth_operator_factory(architecture=architecture)

        # Stage 1 — plan (single LLM call → AuthSpec).
        try:
            spec = planner.plan(
                problem_slice=problem_slice,
                architecture=architecture,
            )
        except Exception as e:
            self._gates.hard(
                "auth_planner_failed",
                f"AuthPlanner raised {type(e).__name__}: {str(e)[:300]}",
            )

        # Stage 2 — apply (deterministic → AuthManifest).
        try:
            manifest: AuthManifest = operator.apply(
                spec=spec,
                primary_app_id=primary_app_id,
                tenant_id=tenant_id,
            )
        except Exception as e:
            self._gates.hard(
                "auth_operator_failed",
                f"FusionAuthOperator raised {type(e).__name__}: "
                f"{str(e)[:300]}",
            )

        # Stage 3 — render contract from live manifest.
        contract_md = render_auth_contract(manifest)

        # Stage 3b — append code samples (small LLM call, optional).
        if self._auth_code_examples_client is not None:
            languages = sorted({
                s.language for s in architecture.services
                if s.service_type in ("backend", "frontend", "worker")
                and (s.language or "").lower() in
                ("python", "typescript", "javascript")
            })
            samples = generate_code_examples(
                client=self._auth_code_examples_client,
                manifest=manifest,
                languages=languages,
                on_status=self._on_status,
            )
            if samples:
                contract_md = contract_md.rstrip() + "\n\n" + samples + "\n"

        # Stage 4 — write contract to disk + emit per-service tests.
        if self._project_root is not None:
            try:
                contract_path = self._project_root / "AUTH_CONTRACT.md"
                contract_path.write_text(contract_md)
                self._log(f"V2Pipeline: wrote {contract_path}")
            except Exception as e:
                self._log(
                    f"V2Pipeline: failed to write AUTH_CONTRACT.md "
                    f"({type(e).__name__}: {e})"
                )
            # Also copy into each service's workspace so any agent that
            # browses the workspace on disk (Coder via view_file,
            # debugger probing files) can read the contract right next
            # to the code. The Coder also gets it via initial-context
            # injection — this is belt-and-suspenders for agents that
            # discover-by-listing.
            for svc in architecture.services:
                if svc.service_type not in ("backend", "frontend", "worker"):
                    continue
                svc_root = self._project_root / svc.workspace_name
                if not svc_root.exists():
                    continue
                try:
                    (svc_root / "AUTH_CONTRACT.md").write_text(contract_md)
                except Exception as e:
                    self._log(
                        f"V2Pipeline: failed to copy AUTH_CONTRACT.md "
                        f"into {svc.workspace_name} "
                        f"({type(e).__name__}: {e})"
                    )
            self._emit_v2_contract_tests(manifest, architecture)

        # Stage 5 — gate on manifest. Deterministic checks.
        critical: list[str] = []
        if not manifest.signing_key.is_rs_family:
            critical.append(
                f"jwt_signing: algorithm={manifest.signing_key.algorithm} "
                f"is not RS-family"
            )
        if not manifest.users:
            critical.append("test_users: zero users in manifest")
        else:
            unverified = [
                u.email for u in manifest.users if not u.login_verified
            ]
            if unverified:
                critical.append(
                    f"token_validation: login failed for "
                    f"{', '.join(unverified)}"
                )
        if critical:
            self._state.mark_top_phase(TopPhase.AUTH, {
                "spec": spec.model_dump(),
                "manifest": manifest.model_dump(),
                "contract_markdown": contract_md,
                "critical": critical,
            })
            self._gates.hard(
                "auth_audit_failed",
                f"FusionAuthOperator manifest failed audit: "
                + "; ".join(critical),
            )

        self._state.mark_top_phase(TopPhase.AUTH, {
            "spec": spec.model_dump(),
            "manifest": manifest.model_dump(),
            "contract_markdown": contract_md,
        })
        return contract_md

    def _emit_v2_contract_tests(
        self,
        manifest: AuthManifest,
        architecture: SystemArchitecture,
    ) -> None:
        """Render tests/auth/test_auth_contract.py per python service.

        Re-uses the existing ``contract_tests`` renderer. Builds a
        contract-markdown-shaped string that the renderer's parsers
        recognize (deterministic-shape: Issuer line + test users
        block in the audit's regex format).
        """
        from bizniz.auth_agent.contract_tests import (
            render_auth_contract_test_file,
        )
        # The renderer parses test users + issuer out of a markdown
        # blob; build the minimal blob that matches its parsers.
        issuer_line = f"- Issuer (iss claim): {manifest.issuer}\n" if manifest.issuer else ""
        users_lines = "\n".join(
            f"- {u.email} / {u.password} — roles {', '.join(u.roles) or 'user'}"
            for u in manifest.users
        )
        synth_md = f"## Issuer\n\n{issuer_line}\n## Test users\n\n{users_lines}\n"

        try:
            content = render_auth_contract_test_file(
                contract_markdown=synth_md,
                primary_app_id=manifest.primary_app_id,
            )
        except Exception as e:
            self._log(
                f"V2Pipeline: contract test render failed "
                f"({type(e).__name__}: {e})"
            )
            return

        for service in architecture.services:
            if (service.language or "").lower() != "python":
                continue
            if (service.service_type or "").lower() not in ("backend", "worker"):
                continue
            try:
                target_dir = (
                    self._project_root / service.workspace_name
                    / "tests" / "auth"
                )
                target_dir.mkdir(parents=True, exist_ok=True)
                init = target_dir / "__init__.py"
                if not init.exists():
                    init.write_text("")
                (target_dir / "test_auth_contract.py").write_text(content)
                self._log(
                    f"V2Pipeline: emitted contract test "
                    f"{service.workspace_name}/tests/auth/test_auth_contract.py"
                )
            except Exception as e:
                self._log(
                    f"V2Pipeline: failed to write contract test for "
                    f"{service.name}: {type(e).__name__}: {e}"
                )

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

        # FusionAuth readiness wait. compose-up reports rc=0 as soon as
        # containers are STARTED, but FA's HTTP API takes ~5–15s after
        # the container starts to become reachable. AuthAgent's
        # preflight runs immediately after compose_up returns, so
        # without this wait the very first FA call hits ConnectionReset.
        # No-op when no FA URL is set (projects without auth).
        self._wait_for_fusionauth(timeout_s=120)

    def _wait_for_fusionauth(self, *, timeout_s: int = 600) -> None:
        """Poll FusionAuth until the api_key actually authenticates,
        or the deadline expires. Two-stage check (mirrors the v1
        FusionAuthOrchestrator.wait_until_fully_ready pattern):

          1. /api/status returns 200 — HTTP server is up
          2. /api/application with the Authorization: <api_key> header
             returns 200 — kickstart has processed the apiKeys block
             and the orchestrator's key actually works

        Stage 1 is necessary but not sufficient; FA reports healthy
        before kickstart finishes. Stage 2 is the real readiness
        signal — without it, AuthAgent's preflight (and the LLM's
        first apiKey call) hits 401/connection-reset and gives up.

        5-second polls, 10-minute default deadline. Generous because
        FA on a slow machine + a fresh kickstart can take 30-60s. We'd
        rather wait than burn an AuthAgent run on a connection reset
        that costs 18-23 LLM calls in an idempotent retry loop.

        Re-reads .env from the project's infra/development first —
        the .env doesn't exist when the pipeline starts on a fresh
        project (provisioner writes it), so the FA URL+key live there
        but not in os.environ.
        """
        import os
        import time
        import urllib.request
        import urllib.error

        # Re-seed FUSIONAUTH_* from the just-written .env. _build_pipeline's
        # initial seed runs at startup before provision, so on a fresh
        # project the env is empty until now.
        slug = self._project_name
        env_path = (
            Path(os.environ.get("BIZNIZ_PROJECTS_ROOT")
                 or str(Path.home() / "bizniz_projects"))
            / slug / "infra" / "development" / ".env"
        )
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("FUSIONAUTH_") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v.strip())

        fa_url = os.environ.get("FUSIONAUTH_HOST_URL")
        api_key = os.environ.get("FUSIONAUTH_API_KEY") or ""
        if not fa_url:
            return

        deadline = time.time() + timeout_s
        start = time.time()
        last_log = 0.0
        stage_1_done = False

        while time.time() < deadline:
            try:
                # Stage 1: HTTP server up
                if not stage_1_done:
                    req = urllib.request.Request(
                        f"{fa_url.rstrip('/')}/api/status", method="GET",
                    )
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        if resp.status == 200:
                            stage_1_done = True
                # Stage 2: api_key works
                if stage_1_done:
                    req = urllib.request.Request(
                        f"{fa_url.rstrip('/')}/api/application", method="GET",
                    )
                    if api_key:
                        req.add_header("Authorization", api_key)
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        if resp.status == 200:
                            self._log(
                                f"V2Pipeline: FusionAuth fully ready at "
                                f"{fa_url} (after {int(time.time() - start)}s)"
                            )
                            return
            except (urllib.error.URLError, urllib.error.HTTPError,
                    ConnectionError, OSError):
                pass
            now = time.time()
            if now - last_log > 5:
                stage = "api_key" if stage_1_done else "http server"
                self._log(
                    f"V2Pipeline: waiting for FusionAuth at {fa_url} "
                    f"({stage}; {int(now - start)}s elapsed)..."
                )
                last_log = now
            time.sleep(5)
        self._log(
            f"V2Pipeline: FusionAuth not fully ready at {fa_url} after "
            f"{timeout_s}s — proceeding anyway (audit will catch)"
        )

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
