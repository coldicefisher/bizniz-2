"""Per-milestone loop with sub-phase resume + repair escalation.

Phases (from state.SubPhase):
  enrich           QualityEngineer.enrich → EnrichedSpec
  implement        Engineer.implement → EngineerResult
  review_initial   QE.review + CodeReviewer.review (parallel-safe; sequential here)
  repair_iter_0/1/2  Engineer.repair with escalating model tier
  review_final     terminal QE.review + CodeReviewer.review
  integration_api  IntegrationPhase.run_api
  integration_web  IntegrationPhase.run_web
  done

Each phase reads/writes its artifact via MilestoneState. ``run()``
walks phases in order, skipping any already complete (resume).
Hard gates in pipeline gates.GatePolicy halt with a GateViolation
if a phase fails terminally; the pipeline catches + persists state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from pydantic import BaseModel, Field

from bizniz.architect.types import SystemArchitecture
from bizniz.code_reviewer.agent import CodeReviewer
from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.driver.gates import GatePolicy, GateViolation
from bizniz.driver.integration_phase import IntegrationPhase, IntegrationPhaseResult
from bizniz.driver.final_tester import FinalTester
from bizniz.driver.smoke_phase import SmokePhase, SmokePhaseResult
from bizniz.driver.smoke_recovery import SmokeRecovery
from bizniz.lib.progress_tracker import ProgressTracker
from bizniz.driver.ux_phase import UXPhase
from bizniz.driver.refactor_phase import RefactorPhase
from bizniz.driver.milestone_code_dispatcher import MilestoneCodeDispatcher
from bizniz.driver.state import MilestoneState, SubPhase, next_subphase
from bizniz.engineer.agent import Engineer
from bizniz.engineer.types import EngineerResult
from bizniz.lib.tool_loop_agent import ToolLoopAgentStalledError
from bizniz.state.issue_store import IssueStateStore
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.agent import QualityEngineer
from bizniz.quality_engineer.types import CoverageReport, EnrichedSpec
from bizniz.workspace.base_workspace import BaseWorkspace


class MilestoneOutcome(BaseModel):
    """Summary returned to the pipeline after a milestone completes."""
    milestone_name: str
    final_subphase: SubPhase
    enriched_spec: Optional[EnrichedSpec] = None
    engineer_result: Optional[EngineerResult] = None
    code_review: Optional[CodeReviewReport] = None
    coverage: Optional[CoverageReport] = None
    repair_iterations: int = 0
    integration_api: Optional[Dict] = None
    integration_worker: Optional[Dict] = None
    integration_web: Optional[Dict] = None
    error_summary: Optional[str] = None


class _ChangedFiles(BaseModel):
    """Helper carrier for changed-file dicts across phases."""
    code_files: Dict[str, str] = Field(default_factory=dict)
    test_files: Dict[str, str] = Field(default_factory=dict)


class MilestoneLoop:
    """Drives one milestone end-to-end with sub-phase resume + repair escalation.

    The Engineer/CodeReviewer/QE instances are passed in at construction
    so the pipeline can swap them per repair iteration (model escalation).
    For most uses a single Engineer is sufficient; pipeline calls
    ``set_engineer_for_iteration(i, engineer)`` between iterations to
    install the next-tier client.
    """

    def __init__(
        self,
        engineer: Engineer,
        quality_engineer: QualityEngineer,
        code_reviewer: CodeReviewer,
        integration_phase: IntegrationPhase,
        smoke_phase: "SmokePhase",
        gates: GatePolicy,
        workspace_for_service: Callable[[str], BaseWorkspace],
        primary_workspace: BaseWorkspace,
        compose_path: str,
        project_root: Path,
        smoke_recovery: Optional["SmokeRecovery"] = None,
        repair_budget: int = 3,
        repair_engineer_factory: Optional[Callable[[int], Engineer]] = None,
        engineer_escalation_factory: Optional[Callable[[int], Engineer]] = None,
        code_dispatcher: Optional[MilestoneCodeDispatcher] = None,
        issue_store_factory: Optional[Callable[[int], IssueStateStore]] = None,
        cost_tracker=None,
        workspace_summary: Optional[str] = None,
        ux_phase: Optional[UXPhase] = None,
        refactor_phase: Optional[RefactorPhase] = None,
        final_tester: Optional["FinalTester"] = None,
        # ``human_docs_generator_factory`` builds a HumanDocsGenerator
        # per milestone (so each gets the right inputs — milestone-
        # scoped capabilities summary, latest compose YAML, latest
        # OpenAPI per service). Wiring lives in v2_build.py.
        human_docs_generator_factory: Optional[Callable] = None,
        total_milestones: Optional[int] = None,
        on_status: Optional[Callable[[str], None]] = None,
        # Confidence-signal thresholds (roadmap item 1). When
        # QualityEngineer.enrich returns confidence < halt threshold,
        # fire the ``enrich_low_confidence`` soft gate; when in the
        # mid-band (halt <= conf < low), run one re-enrich pass and
        # take whichever spec has higher confidence.
        confidence_low_threshold: float = 0.6,
        confidence_halt_threshold: float = 0.4,
        # Progress-based stop threshold for the iterative smoke-recovery
        # loop (D3, 2026-05-17). When the recovery agent makes progress
        # (failures decrease) we keep going; we only stop after this many
        # consecutive no-progress iterations (stalled OR regression).
        # Default 5 matches BiznizConfig.debugger_stall_threshold. Set to
        # 1 to recover legacy single-shot behavior.
        smoke_recovery_stall_threshold: int = 5,
        # Progress-based stop threshold for the review/repair loop
        # (D5, 2026-05-17). Replaces the legacy ``repair_budget`` hard
        # cap. The loop iterates as long as defect count (missing
        # scenarios + critical findings) keeps decreasing; halts after
        # this many consecutive no-progress iterations. Default 5,
        # same shared knob as smoke recovery.
        repair_stall_threshold: int = 5,
        # Safety net: hard upper bound on review/repair iterations.
        # The progress-based stop is the primary mechanism; this is
        # belt-and-suspenders to prevent a pathological
        # "always-progresses-by-one" loop from running forever.
        # Default 20 — well beyond what any realistic milestone needs.
        repair_max_iterations: int = 20,
    ):
        self._engineer = engineer
        self._qe = quality_engineer
        self._cr = code_reviewer
        self._integration = integration_phase
        self._smoke = smoke_phase
        self._smoke_recovery = smoke_recovery
        self._smoke_recovery_stall_threshold = max(1, int(smoke_recovery_stall_threshold))
        self._repair_stall_threshold = max(1, int(repair_stall_threshold))
        self._repair_max_iterations = max(1, int(repair_max_iterations))
        self._confidence_low_threshold = confidence_low_threshold
        self._confidence_halt_threshold = confidence_halt_threshold
        self._ux_phase = ux_phase
        self._refactor_phase = refactor_phase
        self._final_tester = final_tester
        self._human_docs_generator_factory = human_docs_generator_factory
        self._total_milestones = total_milestones
        self._gates = gates
        self._workspace_for_service = workspace_for_service
        self._primary_workspace = primary_workspace
        self._compose_path = compose_path
        self._project_root = project_root
        self._repair_budget = max(0, min(5, repair_budget))
        self._repair_engineer_factory = repair_engineer_factory
        # Escalation factory for IMPLEMENT (called on stall detection).
        # Tier 0 = the default engineer; tier N>0 = next-tier model.
        self._engineer_escalation_factory = engineer_escalation_factory
        # v2.5 dispatcher — when set, supersedes the v2 Engineer for the
        # IMPLEMENT phase. Repair phases still use the v2 Engineer until
        # the v2.5 review-and-repair path is built.
        self._code_dispatcher = code_dispatcher
        # Factory: milestone_index → IssueStateStore. Each milestone gets
        # its own scoped store. When set, IMPLEMENT-phase state lives in
        # the DB (not the per-phase JSON).
        self._issue_store_factory = issue_store_factory
        # Per-call binding so _phase_implement_with_escalation can hand
        # the store to the dispatcher without re-querying the factory
        # (and so we don't have to thread state.milestone_index through
        # the existing private method signatures).
        self._current_milestone_store = None
        self._cost_tracker = cost_tracker
        self._workspace_summary = workspace_summary
        self._on_status = on_status

    def _tag(self, milestone_index: int, phase: SubPhase) -> None:
        """Tag the cost tracker with the current milestone + phase so
        per-call records land in the right bucket."""
        if self._cost_tracker is None:
            return
        try:
            self._cost_tracker.set_milestone(milestone_index)
            self._cost_tracker.set_phase(phase.value)
        except Exception:
            # Cost tracking is best-effort; never break a real run.
            pass

    def _maybe_recover_smoke(
        self,
        smoke_result: SmokePhaseResult,
        milestone,
        architecture,
        auth_contract: Optional[str],
        state,
    ) -> SmokePhaseResult:
        """Iterative agent recovery for a failing smoke phase (D3,
        2026-05-17). Returns the latest smoke result (passing on
        success; the last failing one when the loop stops without
        converging).

        Loop rules (per user direction):
        - One iteration = one SmokeRecovery dispatch + one SmokePhase
          re-run. The re-run is the source of truth, not the agent's
          self-report.
        - ``ProgressTracker`` decides when to stop: as long as the
          critical-failure count keeps going DOWN, we keep going. Stop
          only after ``smoke_recovery_stall_threshold`` consecutive
          no-progress iterations (stalled OR regression) — default 5
          via ``BiznizConfig.debugger_stall_threshold``.
        - Hard short-circuit at convergence (0 critical failures).
        - No-op when ``smoke_recovery`` wasn't injected.
        """
        if self._smoke_recovery is None:
            return smoke_result
        if smoke_result.passed:
            return smoke_result

        service_names = [s.name for s in architecture.services]
        current = smoke_result
        tracker = ProgressTracker(
            initial_failure_count=len(current.critical_failures),
            stall_threshold=self._smoke_recovery_stall_threshold,
        )
        recovery_history: List[dict] = []

        if self._on_status:
            try:
                self._on_status(
                    f"SmokePhase: {len(current.critical_failures)} "
                    f"critical failure(s); entering iterative recovery "
                    f"(stall threshold={self._smoke_recovery_stall_threshold})..."
                )
            except Exception:
                pass

        iteration = 0
        while True:
            iteration += 1
            # Defensive: any agent crash → bail out of the loop with
            # whatever the latest verified result is. Don't bring the
            # pipeline down on a recovery-code bug.
            try:
                recovery_result = self._smoke_recovery.recover(
                    critical_failures=current.critical_failures,
                    service_names=service_names,
                    milestone_title=milestone.name,
                )
            except Exception as e:
                if self._on_status:
                    try:
                        self._on_status(
                            f"SmokeRecovery iter {iteration}: dispatch raised "
                            f"{type(e).__name__}: {e} — stopping recovery loop"
                        )
                    except Exception:
                        pass
                break

            recovery_history.append(recovery_result.model_dump())

            if not recovery_result.attempted:
                # Agent declined to act (e.g. claude binary missing at
                # runtime). No point looping — return current state.
                state.mark_phase(
                    SubPhase.SMOKE,
                    {
                        **current.model_dump(),
                        "recovery_history": recovery_history,
                        "recovery_iterations": iteration,
                    },
                )
                return current

            if self._on_status:
                try:
                    self._on_status(
                        f"SmokeRecovery iter {iteration}: attempted; "
                        f"{len(recovery_result.actions_taken)} action(s), "
                        f"self_reported_ok={recovery_result.succeeded} — "
                        f"re-running smoke for verification..."
                    )
                except Exception:
                    pass

            current = self._smoke.run(
                milestone=milestone,
                architecture=architecture,
                project_root=self._project_root,
                auth_contract=auth_contract,
            )
            verdict = tracker.update(len(current.critical_failures))

            if self._on_status:
                try:
                    self._on_status(
                        f"SmokePhase (after recovery iter {iteration}): "
                        f"{'PASSED' if current.passed else 'still failing'} "
                        f"— {len(current.critical_failures)} critical "
                        f"failure(s); verdict={verdict}, "
                        f"stall_counter={tracker.consecutive_no_progress}/"
                        f"{self._smoke_recovery_stall_threshold}"
                    )
                except Exception:
                    pass

            state.mark_phase(
                SubPhase.SMOKE,
                {
                    **current.model_dump(),
                    "recovery_history": recovery_history,
                    "recovery_iterations": iteration,
                    "progress_history": tracker.render_history(),
                    "after_recovery": True,
                },
            )

            if tracker.has_converged() or current.passed:
                if self._on_status:
                    try:
                        self._on_status(
                            f"SmokeRecovery: converged after {iteration} "
                            f"iteration(s)"
                        )
                    except Exception:
                        pass
                return current

            if tracker.should_stop():
                if self._on_status:
                    try:
                        self._on_status(
                            f"SmokeRecovery: stall threshold "
                            f"({self._smoke_recovery_stall_threshold}) reached "
                            f"after {iteration} iteration(s); halting recovery "
                            f"loop with {len(current.critical_failures)} "
                            f"critical failure(s) remaining"
                        )
                    except Exception:
                        pass
                return current

        # Loop exited via ``break`` (recovery dispatch raised). Return
        # whatever the latest verified result was — the original smoke
        # failure when the very first dispatch crashed, or the most
        # recent re-run result on a later-iteration crash.
        return current

    # ── Public ─────────────────────────────────────────────────────────

    def run(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        prior_specs: Iterable[EnrichedSpec],
        auth_contract: Optional[str],
        state: MilestoneState,
        only_phase: Optional[SubPhase] = None,
    ) -> MilestoneOutcome:
        """Run (or resume) milestone ``milestone`` to completion.

        ``only_phase``: when set, run ONLY that sub-phase (re-running
        even if marked done) and return immediately. Loads required
        prerequisites from disk; halts if a prerequisite phase is
        missing. Useful for ``--phase review --milestone N``-style
        re-entry after editing the spec or prior artifact.
        """
        if only_phase is not None:
            return self._run_single_phase(
                milestone, architecture, list(prior_specs or []),
                auth_contract, state, only_phase,
            )
        return self._run_full(
            milestone, architecture, list(prior_specs or []),
            auth_contract, state,
        )

    def _run_full(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        prior_list,
        auth_contract,
        state: MilestoneState,
    ) -> MilestoneOutcome:
        """Run (or resume) milestone ``milestone`` to completion.

        Resume: any sub-phase already in ``state.completed_phases()`` is
        skipped; the phase artifact is loaded from disk if needed by a
        downstream phase.
        """
        self._log(
            f"MilestoneLoop: M{state.milestone_index} "
            f"'{milestone.name}' (resume from {state.last_completed()})"
        )

        # Phase artifacts assembled in-memory as the loop progresses.
        spec: Optional[EnrichedSpec] = self._load_spec_if_done(state)
        result: Optional[EngineerResult] = self._load_engineer_result_if_done(state)
        coverage: Optional[CoverageReport] = self._load_coverage_if_done(state)
        code_review: Optional[CodeReviewReport] = self._load_review_if_done(state)
        repair_iterations = 0
        integration_api: Optional[IntegrationPhaseResult] = None
        integration_worker: Optional[IntegrationPhaseResult] = None
        integration_web: Optional[IntegrationPhaseResult] = None

        # Walk the phases in order; each branch skips if already done.

        if not _has(state, SubPhase.ENRICH):
            self._tag(state.milestone_index, SubPhase.ENRICH)
            spec = self._phase_enrich(milestone, architecture, auth_contract, prior_list)
            state.mark_phase(SubPhase.ENRICH, spec)
        spec = spec or self._reload_required(state, SubPhase.ENRICH, EnrichedSpec)

        # IMPLEMENT phase: when an issue_store_factory is wired, the DB
        # is the authoritative source for issue-level state. No JSON
        # artifact is written for IMPLEMENT — the dispatcher persists
        # rows as it goes; a resumed run picks up where it left off.
        # We mark the phase complete on disk only as a no-payload marker
        # so downstream resume gates (`_has(state, SubPhase.IMPLEMENT)`)
        # see it. The actual EngineerResult is rebuilt from DB rows on
        # demand via _load_engineer_result_if_done.
        if self._issue_store_factory is not None:
            issue_store = self._issue_store_factory(state.milestone_index)
            if not issue_store.is_implement_done():
                self._tag(state.milestone_index, SubPhase.IMPLEMENT)
                # Stash the per-milestone store so
                # _phase_implement_with_escalation can pass it to the
                # dispatcher. Cleared on exit so subsequent calls are
                # clean.
                self._current_milestone_store = issue_store
                try:
                    result = self._phase_implement_with_escalation(
                        milestone, architecture, spec, auth_contract, prior_list,
                    )
                finally:
                    self._current_milestone_store = None
                # Marker only — payload lives in the DB.
                state.mark_phase(SubPhase.IMPLEMENT, {"_db_backed": True})
            if result is None:
                result = issue_store.assemble_engineer_result()
        else:
            if not _has(state, SubPhase.IMPLEMENT):
                self._tag(state.milestone_index, SubPhase.IMPLEMENT)
                result = self._phase_implement_with_escalation(
                    milestone, architecture, spec, auth_contract, prior_list,
                )
                state.mark_phase(SubPhase.IMPLEMENT, result)
            result = result or self._reload_required(
                state, SubPhase.IMPLEMENT, EngineerResult,
            )

        # Smoke — deterministic curl gate. Cheap; no LLM. Catches
        # the v33-class bug where the milestone shipped "green" but
        # the live container 500s or auth is misconfigured. Hard-gate
        # on critical failures (health, auth_login, route 5xx).
        if not _has(state, SubPhase.SMOKE):
            self._tag(state.milestone_index, SubPhase.SMOKE)
            smoke_result = self._smoke.run(
                milestone=milestone,
                architecture=architecture,
                project_root=self._project_root,
                auth_contract=auth_contract,
            )
            state.mark_phase(SubPhase.SMOKE, smoke_result.model_dump())
            if not smoke_result.passed:
                # Recovery attempt before hard-halt (2026-05-15 ask):
                # most smoke 5xx are state drift (stale uvicorn, cached
                # bundle, missing migration) — a one-shot Claude session
                # with Bash + Edit can restart/inspect/fix in 30s and
                # let the pipeline continue. If the re-probe still
                # fails after recovery, we hard-halt as before.
                smoke_result = self._maybe_recover_smoke(
                    smoke_result=smoke_result,
                    milestone=milestone,
                    architecture=architecture,
                    auth_contract=auth_contract,
                    state=state,
                )
            if not smoke_result.passed:
                self._gates.hard(
                    "smoke_failed",
                    f"smoke phase critical failures: "
                    f"{'; '.join(smoke_result.critical_failures[:3])}"
                    + (
                        f" (+{len(smoke_result.critical_failures) - 3} more)"
                        if len(smoke_result.critical_failures) > 3 else ""
                    ),
                )

        # Review + repair: a single progress-based loop (2026-05-17,
        # D5). Runs QE + CR review; if either un-approves, dispatch
        # Engineer.repair and re-review. Loop iterates as long as the
        # defect count (missing scenarios + critical findings) keeps
        # decreasing; stops on approval (convergence) or after
        # ``repair_stall_threshold`` consecutive no-progress iterations.
        # No hard iteration cap — ProgressTracker is the safety mechanism.
        if not self._review_repair_done(state):
            coverage, code_review, result, repair_iterations, history = (
                self._phase_review_repair_loop(
                    state=state,
                    milestone=milestone,
                    architecture=architecture,
                    spec=spec,
                    initial_result=result,
                    auth_contract=auth_contract,
                    prior_list=prior_list,
                )
            )
            state.mark_phase(SubPhase.REVIEW_REPAIR, {
                "coverage": coverage.model_dump() if coverage else None,
                "code_review": code_review.model_dump() if code_review else None,
                "repair_iterations": repair_iterations,
                "progress_history": history,
            })
            if not self._approved(coverage, code_review):
                self._gates.hard(
                    "milestone_unapproved",
                    f"M{state.milestone_index} '{milestone.name}' not "
                    f"approved after {repair_iterations} repair "
                    f"iteration(s). "
                    f"coverage.approved={coverage and coverage.approved}, "
                    f"code_review.approved={code_review and code_review.approved}",
                )

        # Integration phases (api → worker → web).
        if not _has(state, SubPhase.INTEGRATION_API):
            self._tag(state.milestone_index, SubPhase.INTEGRATION_API)
            integration_api = self._integration.run_api(
                milestone=milestone,
                architecture=architecture,
                project_root=self._project_root,
                compose_path=self._compose_path,
                service_workspaces={
                    s.name: self._workspace_for_service(s.name)
                    for s in architecture.services
                },
                auth_contract=auth_contract,
            )
            state.mark_phase(SubPhase.INTEGRATION_API, integration_api)
            if not integration_api.passed:
                self._gates.hard(
                    "integration_api_failed",
                    integration_api.error_summary or "API integration tests failed",
                )

        if not _has(state, SubPhase.INTEGRATION_WORKER):
            self._tag(state.milestone_index, SubPhase.INTEGRATION_WORKER)
            api_artifact = state.read_artifact(SubPhase.INTEGRATION_API) or {}
            backend_contracts = api_artifact.get("backend_contracts") or {}
            integration_worker = self._integration.run_worker(
                milestone=milestone,
                architecture=architecture,
                project_root=self._project_root,
                compose_path=self._compose_path,
                service_workspaces={
                    s.name: self._workspace_for_service(s.name)
                    for s in architecture.services
                },
                backend_contracts=backend_contracts,
                auth_contract=auth_contract,
            )
            state.mark_phase(SubPhase.INTEGRATION_WORKER, integration_worker)
            if not integration_worker.passed:
                self._gates.hard(
                    "integration_worker_failed",
                    integration_worker.error_summary or "Worker integration tests failed",
                )

        if not _has(state, SubPhase.INTEGRATION_WEB):
            self._tag(state.milestone_index, SubPhase.INTEGRATION_WEB)
            api_artifact = state.read_artifact(SubPhase.INTEGRATION_API) or {}
            backend_contracts = api_artifact.get("backend_contracts") or {}
            integration_web = self._integration.run_web(
                milestone=milestone,
                architecture=architecture,
                project_root=self._project_root,
                compose_path=self._compose_path,
                service_workspaces={
                    s.name: self._workspace_for_service(s.name)
                    for s in architecture.services
                },
                backend_contracts=backend_contracts,
                auth_contract=auth_contract,
            )
            state.mark_phase(SubPhase.INTEGRATION_WEB, integration_web)
            if not integration_web.passed:
                self._gates.hard(
                    "integration_web_failed",
                    integration_web.error_summary or "Web integration tests failed",
                )

        # ── Post-integration smoke ─────────────────────────────────────
        # Re-run the smoke gate AFTER integration phases to catch state
        # drift that integration tests can introduce — e.g. a
        # ``Base.metadata.drop_all`` fixture that leaves the production
        # DB tables-less, or a backend restart whose lifespan's
        # ``create_all`` silently fails. The pre-implement smoke gate
        # (above) caught the "implement phase shipped broken code"
        # class; this one catches the "integration cleanup broke the
        # running stack" class that surfaced on crm_v1 M5 (2026-05-16).
        # No state-schema change — this is a transient verification,
        # not a resumable checkpoint.
        self._log(
            "MilestoneLoop: post-integration smoke verification..."
        )
        post_smoke = self._smoke.run(
            milestone=milestone,
            architecture=architecture,
            project_root=self._project_root,
            auth_contract=auth_contract,
        )
        if not post_smoke.passed:
            self._gates.hard(
                "post_integration_smoke_failed",
                f"Post-integration smoke regressed "
                f"({len(post_smoke.critical_failures)} critical "
                f"failure(s)): "
                f"{'; '.join(post_smoke.critical_failures[:3])}",
            )
        self._log(
            f"MilestoneLoop: post-integration smoke passed "
            f"({len(post_smoke.checks)} check(s))"
        )

        # ── UX_REVIEW ──────────────────────────────────────────────────
        # Runs after the milestone's frontend integration verified the
        # service is actually reachable. Self-skips when no frontend or
        # no ux_factory is wired.
        ux_result = None
        if not _has(state, SubPhase.UX_REVIEW):
            self._tag(state.milestone_index, SubPhase.UX_REVIEW)
            if self._ux_phase is not None:
                ux_result = self._ux_phase.run(
                    milestone=milestone,
                    architecture=architecture,
                    project_root=self._project_root,
                    service_workspaces={
                        s.name: self._workspace_for_service(s.name)
                        for s in architecture.services
                    },
                    compose_path=self._compose_path,
                    auth_contract=auth_contract,
                )
            state.mark_phase(
                SubPhase.UX_REVIEW,
                ux_result.model_dump() if ux_result is not None
                else {"skipped_reason": "no ux_phase wired"},
            )

        # ── REFACTOR ───────────────────────────────────────────────────
        # Runs when the Planner flagged this milestone OR it's the final
        # milestone (always treated as a refactor boundary). Currently a
        # stub; real Refactorer ships in Stage 2.
        refactor_result = None
        if not _has(state, SubPhase.REFACTOR):
            self._tag(state.milestone_index, SubPhase.REFACTOR)
            should_refactor = (
                getattr(milestone, "refactor_after", False)
                or self._is_final_milestone(milestone)
            )
            if should_refactor and self._refactor_phase is not None:
                refactor_result = self._refactor_phase.run(
                    milestone=milestone,
                    architecture=architecture,
                    project_root=self._project_root,
                    service_workspaces={
                        s.name: self._workspace_for_service(s.name)
                        for s in architecture.services
                    },
                    is_final_milestone=self._is_final_milestone(milestone),
                )
            state.mark_phase(
                SubPhase.REFACTOR,
                refactor_result.model_dump() if refactor_result is not None
                else {
                    "skipped_reason": (
                        "refactor_after=False and not final milestone"
                        if not should_refactor else "no refactor_phase wired"
                    ),
                },
            )

        # ── DOCUMENT ────────────────────────────────────────────────────
        # Generate human-readable docs (README, architecture, api/,
        # services/, milestones/) into <project>/docs/. Hybrid:
        # deterministic for structured data, LLM for narrative.
        # Failure is RECORDED but doesn't gate the milestone — docs
        # are best-effort, and the operator can re-run later.
        if not _has(state, SubPhase.DOCUMENT):
            self._tag(state.milestone_index, SubPhase.DOCUMENT)
            if self._human_docs_generator_factory is not None:
                try:
                    generator = self._human_docs_generator_factory(
                        milestone=milestone,
                        architecture=architecture,
                        auth_contract=auth_contract,
                    )
                    docs_result = generator.run()
                    state.mark_phase(
                        SubPhase.DOCUMENT, docs_result.model_dump(),
                    )
                    self._log(
                        f"MilestoneLoop: docs generated — "
                        f"{docs_result.succeeded_count()}/"
                        f"{len(docs_result.docs)} file(s) succeeded"
                    )
                except Exception as e:
                    state.mark_phase(SubPhase.DOCUMENT, {
                        "skipped_reason": (
                            f"docs generator raised "
                            f"{type(e).__name__}: {e}"
                        ),
                    })
                    self._log(
                        f"MilestoneLoop: docs phase raised "
                        f"{type(e).__name__}: {e} — continuing"
                    )
            else:
                state.mark_phase(SubPhase.DOCUMENT, {
                    "skipped_reason": "no human_docs_generator_factory wired",
                })

        # ── FINAL_TEST ──────────────────────────────────────────────────
        # End-of-milestone e2e canary — the LAST gate before DONE.
        # Verifies the stack is shippable by hitting real HTTP
        # endpoints as a user would. No fixtures, no test data — just
        # confirms the running services respond on their happy paths.
        # Catches damage from any prior phase (integration teardown,
        # refactor extracts that broke imports, UX fixes that
        # mis-wired a route).
        if not _has(state, SubPhase.FINAL_TEST):
            self._tag(state.milestone_index, SubPhase.FINAL_TEST)
            if self._final_tester is not None:
                final_result = self._final_tester.run(
                    milestone=milestone,
                    architecture=architecture,
                    project_root=self._project_root,
                    auth_contract=auth_contract,
                )
                state.mark_phase(
                    SubPhase.FINAL_TEST, final_result.model_dump(),
                )
                if not final_result.passed:
                    self._gates.hard(
                        "final_test_failed",
                        f"FinalTester regressed "
                        f"({len(final_result.critical_failures)} critical "
                        f"failure(s)): "
                        f"{'; '.join(final_result.critical_failures[:3])}",
                    )
            else:
                state.mark_phase(
                    SubPhase.FINAL_TEST,
                    {"skipped_reason": "no final_tester wired"},
                )

        state.mark_phase(SubPhase.DONE)
        self._log(
            f"MilestoneLoop: M{state.milestone_index} '{milestone.name}' DONE "
            f"({repair_iterations} repair iterations)"
        )

        return MilestoneOutcome(
            milestone_name=milestone.name,
            final_subphase=SubPhase.DONE,
            enriched_spec=spec,
            engineer_result=result,
            code_review=code_review,
            coverage=coverage,
            repair_iterations=repair_iterations,
            integration_api=integration_api.model_dump() if integration_api else None,
            integration_worker=integration_worker.model_dump() if integration_worker else None,
            integration_web=integration_web.model_dump() if integration_web else None,
        )

    # ── Single-phase re-entry (for --phase flag) ────────────────────────

    def _run_single_phase(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        prior_list,
        auth_contract: Optional[str],
        state: MilestoneState,
        target: SubPhase,
    ) -> MilestoneOutcome:
        """Run only ``target`` for this milestone, loading prerequisite
        artifacts from disk. Re-runs the phase even if already marked
        done. Does NOT mark DONE at the end.

        Halts via gates if a required prerequisite is missing.
        """
        self._log(
            f"MilestoneLoop: M{state.milestone_index} '{milestone.name}' "
            f"single-phase re-entry: {target.value}"
        )

        # Load prerequisites from disk for any phase that needs them.
        spec = self._reload_required(state, SubPhase.ENRICH, EnrichedSpec) \
            if target != SubPhase.ENRICH else None
        result = None
        if target in (SubPhase.SMOKE, SubPhase.REVIEW_REPAIR,
                      SubPhase.REVIEW_INITIAL,
                      SubPhase.REPAIR_ITER_0,
                      SubPhase.REPAIR_ITER_1, SubPhase.REPAIR_ITER_2,
                      SubPhase.REVIEW_FINAL,
                      SubPhase.INTEGRATION_API, SubPhase.INTEGRATION_WORKER,
                      SubPhase.INTEGRATION_WEB):
            # DB-backed when configured; legacy JSON otherwise.
            if self._issue_store_factory is not None:
                store = self._issue_store_factory(state.milestone_index)
                if not store.is_implement_done():
                    self._gates.hard(
                        "missing_state_artifact",
                        f"phase {target.value} requires IMPLEMENT but DB has "
                        f"no terminal coder_issues for this milestone",
                    )
                result = store.assemble_engineer_result()
            else:
                result = self._reload_required(
                    state, SubPhase.IMPLEMENT, EngineerResult,
                )

        # Dispatch.
        coverage: Optional[CoverageReport] = None
        code_review: Optional[CodeReviewReport] = None
        integration_api: Optional[IntegrationPhaseResult] = None
        integration_worker: Optional[IntegrationPhaseResult] = None
        integration_web: Optional[IntegrationPhaseResult] = None
        repair_iterations = 0

        self._tag(state.milestone_index, target)

        if target == SubPhase.ENRICH:
            spec = self._phase_enrich(milestone, architecture, auth_contract, prior_list)
            state.mark_phase(target, spec)

        elif target == SubPhase.IMPLEMENT:
            # Mirror _run_full's IMPLEMENT branch: stash the per-
            # milestone store on self so _phase_implement_with_escalation
            # passes it to the dispatcher (where skip_planning needs
            # it to be non-None to honor --retry-failed).
            store_for_implement = (
                self._issue_store_factory(state.milestone_index)
                if self._issue_store_factory is not None else None
            )
            self._current_milestone_store = store_for_implement
            try:
                result = self._phase_implement(
                    milestone, architecture, spec, auth_contract, prior_list,
                )
            finally:
                self._current_milestone_store = None
            state.mark_phase(target, result)

        elif target == SubPhase.SMOKE:
            smoke_result = self._smoke.run(
                milestone=milestone,
                architecture=architecture,
                project_root=self._project_root,
                auth_contract=auth_contract,
            )
            state.mark_phase(target, smoke_result.model_dump())
            if not smoke_result.passed:
                self._gates.hard(
                    "smoke_failed",
                    f"smoke phase critical failures: "
                    f"{'; '.join(smoke_result.critical_failures[:3])}",
                )

        elif target == SubPhase.REVIEW_REPAIR:
            # Re-run the entire iterative review/repair loop from
            # scratch. Useful after a prompt/model change to verify
            # the milestone re-converges with the new agent.
            coverage, code_review, result, repair_iterations, history = (
                self._phase_review_repair_loop(
                    state=state,
                    milestone=milestone,
                    architecture=architecture,
                    spec=spec,
                    initial_result=result,
                    auth_contract=auth_contract,
                    prior_list=prior_list,
                )
            )
            state.mark_phase(target, {
                "coverage": coverage.model_dump() if coverage else None,
                "code_review": code_review.model_dump() if code_review else None,
                "repair_iterations": repair_iterations,
                "progress_history": history,
            })

        elif target in (SubPhase.REVIEW_INITIAL, SubPhase.REVIEW_FINAL):
            # Legacy single-review re-entry: just run a review, no
            # repair loop. Kept for tooling that targets the old
            # phase names; new code should use REVIEW_REPAIR.
            coverage, code_review = self._phase_review(
                milestone, architecture, spec, result, auth_contract, prior_list,
            )
            state.mark_phase(target, {
                "coverage": coverage.model_dump(),
                "code_review": code_review.model_dump(),
            })

        elif target in (SubPhase.REPAIR_ITER_0, SubPhase.REPAIR_ITER_1, SubPhase.REPAIR_ITER_2):
            iter_idx = {
                SubPhase.REPAIR_ITER_0: 0,
                SubPhase.REPAIR_ITER_1: 1,
                SubPhase.REPAIR_ITER_2: 2,
            }[target]
            # Need the latest review verdict to feed the repair report.
            latest_cov = self._load_coverage_if_done(state)
            latest_cr = self._load_review_if_done(state)
            if latest_cov is None or latest_cr is None:
                self._gates.hard(
                    "missing_review_for_repair",
                    f"cannot run {target.value} without a prior review on disk",
                )
            engineer_for_repair = self._engineer_for_repair(iter_idx)
            report_for_repair = _merge_to_repair_report(
                milestone.name, latest_cov, latest_cr,
            )
            result = engineer_for_repair.repair(
                milestone=milestone,
                architecture=architecture,
                code_review_report=report_for_repair,
                enriched_spec=spec,
                auth_contract=auth_contract,
                prior_specs=prior_list,
            )
            repair_iterations = 1
            coverage, code_review = self._phase_review(
                milestone, architecture, spec, result, auth_contract, prior_list,
            )
            state.mark_phase(target, {
                "engineer_result": result.model_dump(),
                "coverage": coverage.model_dump(),
                "code_review": code_review.model_dump(),
            })

        elif target == SubPhase.INTEGRATION_API:
            integration_api = self._integration.run_api(
                milestone=milestone, architecture=architecture,
                project_root=self._project_root, compose_path=self._compose_path,
                service_workspaces={
                    s.name: self._workspace_for_service(s.name)
                    for s in architecture.services
                },
                auth_contract=auth_contract,
            )
            state.mark_phase(target, integration_api)
            # Loud reminder: single-phase mode finished THIS phase
            # but the milestone is not done until WORKER and WEB
            # also run. Easy thing to overlook — and it bit us on
            # property_manager_claude (saw passing integration_api
            # and assumed M1 was demo-ready; the SPA was still
            # broken because integration_web never executed).
            has_worker = any(
                (s.service_type or "").lower() == "worker"
                for s in architecture.services
            )
            has_frontend = any(
                (s.service_type or "").lower() == "frontend"
                for s in architecture.services
            )
            pending = []
            if has_worker:
                pending.append("integration_worker")
            if has_frontend:
                pending.append("integration_web")
            if pending:
                self._log(
                    f"MilestoneLoop: M{state.milestone_index} "
                    f"NOT DONE — single-phase mode ran integration_api "
                    f"only; still pending: {', '.join(pending)}. "
                    f"Run without ``--phase`` to chain through all "
                    f"integration phases, or fire each phase separately."
                )

        elif target == SubPhase.INTEGRATION_WORKER:
            api_artifact = state.read_artifact(SubPhase.INTEGRATION_API) or {}
            backend_contracts = api_artifact.get("backend_contracts") or {}
            integration_worker = self._integration.run_worker(
                milestone=milestone, architecture=architecture,
                project_root=self._project_root, compose_path=self._compose_path,
                service_workspaces={
                    s.name: self._workspace_for_service(s.name)
                    for s in architecture.services
                },
                backend_contracts=backend_contracts,
                auth_contract=auth_contract,
            )
            state.mark_phase(target, integration_worker)

        elif target == SubPhase.INTEGRATION_WEB:
            api_artifact = state.read_artifact(SubPhase.INTEGRATION_API) or {}
            backend_contracts = api_artifact.get("backend_contracts") or {}
            integration_web = self._integration.run_web(
                milestone=milestone, architecture=architecture,
                project_root=self._project_root, compose_path=self._compose_path,
                service_workspaces={
                    s.name: self._workspace_for_service(s.name)
                    for s in architecture.services
                },
                backend_contracts=backend_contracts,
                auth_contract=auth_contract,
            )
            state.mark_phase(target, integration_web)

        else:
            self._gates.hard(
                "invalid_target_phase",
                f"--phase {target.value} is not addressable as a single phase",
            )

        return MilestoneOutcome(
            milestone_name=milestone.name,
            final_subphase=target,
            enriched_spec=spec,
            engineer_result=result,
            code_review=code_review,
            coverage=coverage,
            repair_iterations=repair_iterations,
            integration_api=integration_api.model_dump() if integration_api else None,
            integration_worker=integration_worker.model_dump() if integration_worker else None,
            integration_web=integration_web.model_dump() if integration_web else None,
        )

    # ── Phase implementations ───────────────────────────────────────────

    def _phase_enrich(
        self, milestone, architecture, auth_contract, prior_list,
    ) -> EnrichedSpec:
        spec = self._qe.enrich(
            milestone=milestone,
            architecture=architecture,
            auth_contract=auth_contract,
            prior_specs=prior_list,
        )
        return self._maybe_re_enrich(
            spec=spec,
            milestone=milestone,
            architecture=architecture,
            auth_contract=auth_contract,
            prior_list=prior_list,
        )

    def _maybe_re_enrich(
        self,
        spec: EnrichedSpec,
        milestone,
        architecture,
        auth_contract,
        prior_list,
    ) -> EnrichedSpec:
        """Confidence-signal load-bearing logic (roadmap item 1).

        Three bands based on ``spec.confidence``:
          - ``>= low_threshold`` (default 0.6): return as-is.
          - ``halt_threshold <= conf < low_threshold`` (default
            0.4-0.6): run ONE re-enrich pass with the augmented prompt,
            return whichever has higher confidence.
          - ``< halt_threshold`` (default < 0.4): fire the
            ``enrich_low_confidence`` soft gate. In ``--auto`` /
            ``strict`` mode this warns and returns the original spec;
            in ``--interactive`` mode it halts for human review.
        """
        if spec.confidence >= self._confidence_low_threshold:
            return spec
        if spec.confidence < self._confidence_halt_threshold:
            # Soft gate. ``--interactive`` halts; otherwise warns +
            # returns the low-confidence spec so the build proceeds.
            self._gates.soft(
                "enrich_low_confidence",
                f"enrich confidence {spec.confidence:.2f} < halt "
                f"threshold {self._confidence_halt_threshold:.2f}; "
                f"spec may be unreliable for milestone "
                f"{milestone.name!r}",
            )
            return spec
        # Mid-band: one re-enrich attempt with the augmented prompt.
        if self._on_status:
            try:
                self._on_status(
                    f"QualityEngineer: enrich confidence "
                    f"{spec.confidence:.2f} in re-enrich band "
                    f"[{self._confidence_halt_threshold:.2f}, "
                    f"{self._confidence_low_threshold:.2f}); "
                    f"running augmented pass..."
                )
            except Exception:
                pass
        try:
            retry_spec = self._qe.re_enrich(
                milestone=milestone,
                prior_spec=spec,
                architecture=architecture,
                auth_contract=auth_contract,
                prior_specs=prior_list,
            )
        except Exception as e:
            # Re-enrich raising should never tank the pipeline — fall
            # back to the original low-confidence spec. The Engineer
            # sees it + the (potentially incomplete) notes; the soft
            # gate's "may be unreliable" warning surfaces in logs.
            if self._on_status:
                try:
                    self._on_status(
                        f"QualityEngineer.re_enrich raised "
                        f"{type(e).__name__}: {e}; sticking with "
                        f"prior spec (confidence={spec.confidence:.2f})"
                    )
                except Exception:
                    pass
            return spec
        if retry_spec.confidence > spec.confidence:
            return retry_spec
        return spec

    def _phase_implement(
        self, milestone, architecture, spec, auth_contract, prior_list,
    ) -> EngineerResult:
        return self._phase_implement_with_escalation(
            milestone, architecture, spec, auth_contract, prior_list,
        )

    def _phase_implement_with_escalation(
        self, milestone, architecture, spec, auth_contract, prior_list,
    ) -> EngineerResult:
        """Run the IMPLEMENT phase.

        v2.5 path: if a ``code_dispatcher`` was injected, route through
        ServicePlanner → Orchestrator → Coder. Stall handling is
        per-issue inside the Orchestrator, so we don't wrap this branch
        in the escalation loop below.

        v2 path (fallback): try the default Engineer; on stall, escalate
        through ``engineer_escalation_factory`` tiers (1, 2, ...) until
        one converges or we exhaust the chain.
        """
        if self._code_dispatcher is not None:
            self._log("MilestoneLoop: implement via v2.5 code dispatcher")
            store = None
            if self._issue_store_factory is not None:
                # _phase_implement_with_escalation runs once per milestone;
                # we don't have direct access to milestone_index here, so
                # close over it via the calling site. The simpler path:
                # rely on the dispatcher's constructor-set store. But we
                # have a factory keyed by milestone_index. Read from the
                # implicit binding the caller set up.
                store = self._current_milestone_store
            return self._code_dispatcher.run(
                architecture=architecture,
                enriched_spec=spec,
                auth_contract=auth_contract,
                workspace_summary=self._workspace_summary,
                issue_store=store,
            )

        # Tier 0 = self._engineer (default model).
        attempt = 0
        last_engineer = self._engineer
        while True:
            try:
                return last_engineer.implement(
                    milestone=milestone,
                    architecture=architecture,
                    enriched_spec=spec,
                    auth_contract=auth_contract,
                    prior_specs=prior_list,
                    workspace_summary=self._workspace_summary,
                )
            except ToolLoopAgentStalledError as e:
                attempt += 1
                self._log(
                    f"MilestoneLoop: implement stalled "
                    f"({e.last_action}); escalating to tier {attempt}"
                )
                if self._engineer_escalation_factory is None:
                    self._gates.hard(
                        "engineer_stalled_no_escalation",
                        f"implement stalled and no escalation factory configured",
                    )
                try:
                    last_engineer = self._engineer_escalation_factory(attempt)
                except IndexError:
                    self._gates.hard(
                        "engineer_escalation_exhausted",
                        f"implement stalled at top tier {attempt}; "
                        f"giving up (no higher tier)",
                    )
                except Exception as e2:
                    self._gates.hard(
                        "engineer_escalation_factory_failed",
                        f"escalation factory raised "
                        f"{type(e2).__name__}: {e2}",
                    )

    def _phase_review(
        self, milestone, architecture, spec, result, auth_contract, prior_list,
    ) -> tuple[CoverageReport, CodeReviewReport]:
        # Snapshot current workspace state per service, build changed-file
        # dicts. For a first pass we read all files declared in the
        # plan's target_files + test_files. Engineer's plan is the
        # authoritative list of what was written.
        all_target = []
        all_test = []
        for issue in result.plan.issues:
            all_target.extend(issue.target_files)
            all_test.extend(issue.test_files)

        code_files: Dict[str, str] = {}
        test_files: Dict[str, str] = {}
        for path in dict.fromkeys(all_target):
            content = _safe_read(self._primary_workspace, path)
            if content is not None:
                code_files[path] = content
        for path in dict.fromkeys(all_test):
            content = _safe_read(self._primary_workspace, path)
            if content is not None:
                test_files[path] = content

        coverage = self._qe.review(
            milestone=milestone,
            enriched_spec=spec,
            engineer_plan=result.plan.model_dump(),
            test_files=test_files,
            auth_contract=auth_contract,
        )
        code_review = self._cr.review(
            milestone=milestone,
            enriched_spec=spec,
            changed_files=code_files,
            architecture=architecture,
            auth_contract=auth_contract,
            prior_specs=prior_list,
        )
        return coverage, code_review

    # ── Repair engineer factory ─────────────────────────────────────────

    def _engineer_for_repair(self, iteration: int) -> Engineer:
        """Return the Engineer instance to use for repair iteration ``iteration``.

        If a ``repair_engineer_factory`` was injected (model escalation
        configured), call it. Otherwise reuse the default Engineer.
        With progress-based iteration (D5), ``iteration`` may exceed
        the configured escalation tier count — the factory itself is
        expected to clamp to its highest tier.
        """
        if self._repair_engineer_factory is not None:
            try:
                return self._repair_engineer_factory(iteration)
            except Exception as e:
                self._log(
                    f"MilestoneLoop: repair_engineer_factory raised "
                    f"{type(e).__name__}: {e} — falling back to default engineer"
                )
        return self._engineer

    def _review_repair_done(self, state: MilestoneState) -> bool:
        """True when review+repair is complete for this milestone.

        Backward-compat: a pre-2026-05-17 state file has ``review_final``
        (legacy terminal review) instead of ``review_repair``. Treat
        either as "review/repair done" so resume on existing builds
        doesn't redo work.
        """
        return _has(state, SubPhase.REVIEW_REPAIR) or _has(
            state, SubPhase.REVIEW_FINAL,
        )

    @staticmethod
    def _defect_count(
        coverage: Optional[CoverageReport],
        code_review: Optional[CodeReviewReport],
    ) -> int:
        """Combined defect signal that the repair loop is trying to
        drive to zero.

        - ``coverage.missing_scenarios`` — capabilities not yet tested
        - ``code_review.critical_findings`` — bugs the reviewer flagged
          as blocking

        Non-critical CR findings (style, recommendations) don't gate
        the milestone and are excluded from the signal so the loop
        doesn't churn on noise. Lower is better; 0 means both
        reviewers approved.
        """
        if coverage is None or code_review is None:
            return 0
        return (
            len(coverage.missing_scenarios)
            + len(code_review.critical_findings)
        )

    def _phase_review_repair_loop(
        self,
        *,
        state: MilestoneState,
        milestone: Milestone,
        architecture: SystemArchitecture,
        spec: EnrichedSpec,
        initial_result: EngineerResult,
        auth_contract: Optional[str],
        prior_list: List[EnrichedSpec],
    ):
        """Run the iterative review/repair loop and return the final
        ``(coverage, code_review, result, iteration_count, history_str)``.

        Iteration 0 = the initial review (no repair). Each subsequent
        iteration dispatches Engineer.repair + a fresh review. The
        loop exits on approval (success) or after
        ``self._repair_stall_threshold`` consecutive no-progress
        iterations (failure — caller fires the milestone_unapproved
        gate). Hard cap also via ``_repair_max_iterations`` so a
        pathological "always-progresses-by-one" agent can't loop
        forever.
        """
        from bizniz.lib.progress_tracker import ProgressTracker

        self._tag(state.milestone_index, SubPhase.REVIEW_REPAIR)

        # Initial review (counts as iteration 0; no repair yet).
        coverage, code_review = self._phase_review(
            milestone, architecture, spec, initial_result,
            auth_contract, prior_list,
        )
        result = initial_result
        if self._approved(coverage, code_review):
            self._log(
                f"MilestoneLoop: review/repair approved on initial "
                f"review (0 repair iterations)"
            )
            return coverage, code_review, result, 0, ""

        tracker = ProgressTracker(
            initial_failure_count=self._defect_count(coverage, code_review),
            stall_threshold=self._repair_stall_threshold,
        )
        self._log(
            f"MilestoneLoop: entering review/repair loop "
            f"(initial defects={tracker.current_failure_count}, "
            f"stall threshold={self._repair_stall_threshold}, "
            f"hard cap={self._repair_max_iterations})"
        )

        repair_iterations = 0
        while True:
            repair_iterations += 1
            iter_idx = repair_iterations - 1  # 0-based for engineer factory
            self._log(
                f"MilestoneLoop: repair iteration {repair_iterations} "
                f"(escalation tier {iter_idx}, defects "
                f"{tracker.current_failure_count})"
            )

            # Dispatch repair — v2.5 dispatcher when wired, v2 Engineer
            # otherwise. Same shape as the legacy loop.
            if self._code_dispatcher is not None:
                store = (
                    self._issue_store_factory(state.milestone_index)
                    if self._issue_store_factory is not None else None
                )
                result = self._code_dispatcher.repair(
                    architecture=architecture,
                    enriched_spec=spec,
                    coverage_report=coverage,
                    code_review_report=code_review,
                    repair_iteration=repair_iterations,
                    auth_contract=auth_contract,
                    workspace_summary=self._workspace_summary,
                    issue_store=store,
                )
            else:
                engineer_for_repair = self._engineer_for_repair(iter_idx)
                report_for_repair = _merge_to_repair_report(
                    milestone.name, coverage, code_review,
                )
                result = engineer_for_repair.repair(
                    milestone=milestone,
                    architecture=architecture,
                    code_review_report=report_for_repair,
                    enriched_spec=spec,
                    auth_contract=auth_contract,
                    prior_specs=prior_list,
                )

            # Re-review with the updated result.
            coverage, code_review = self._phase_review(
                milestone, architecture, spec, result,
                auth_contract, prior_list,
            )

            if self._approved(coverage, code_review):
                self._log(
                    f"MilestoneLoop: review/repair approved after "
                    f"{repair_iterations} repair iteration(s)"
                )
                return (
                    coverage, code_review, result,
                    repair_iterations, tracker.render_history(),
                )

            verdict = tracker.update(self._defect_count(coverage, code_review))
            self._log(
                f"MilestoneLoop: review/repair iter {repair_iterations}: "
                f"verdict={verdict}, defects={tracker.current_failure_count}, "
                f"stall_counter={tracker.consecutive_no_progress}/"
                f"{self._repair_stall_threshold}"
            )

            if tracker.should_stop():
                self._log(
                    f"MilestoneLoop: review/repair stall threshold "
                    f"reached after {repair_iterations} iteration(s) — "
                    f"halting loop with {tracker.current_failure_count} "
                    f"defect(s) remaining"
                )
                return (
                    coverage, code_review, result,
                    repair_iterations, tracker.render_history(),
                )

            if repair_iterations >= self._repair_max_iterations:
                self._log(
                    f"MilestoneLoop: review/repair hard cap "
                    f"({self._repair_max_iterations}) reached — "
                    f"halting loop"
                )
                return (
                    coverage, code_review, result,
                    repair_iterations, tracker.render_history(),
                )

    # ── Helpers ────────────────────────────────────────────────────────

    def _approved(
        self,
        coverage: Optional[CoverageReport],
        code_review: Optional[CodeReviewReport],
    ) -> bool:
        if coverage is None or code_review is None:
            return False
        return bool(coverage.approved and code_review.approved)

    def _load_spec_if_done(self, state: MilestoneState) -> Optional[EnrichedSpec]:
        if not _has(state, SubPhase.ENRICH):
            return None
        return self._reload_required(state, SubPhase.ENRICH, EnrichedSpec)

    def _load_engineer_result_if_done(self, state: MilestoneState) -> Optional[EngineerResult]:
        # DB-backed path: assemble from coder_issues rows.
        if self._issue_store_factory is not None:
            issue_store = self._issue_store_factory(state.milestone_index)
            if issue_store.is_implement_done():
                return issue_store.assemble_engineer_result()
            return None
        # Legacy JSON path.
        if not _has(state, SubPhase.IMPLEMENT):
            return None
        return self._reload_required(state, SubPhase.IMPLEMENT, EngineerResult)

    # Lookback chain for finding the latest review artifact across the
    # 2026-05-17 phase rename. New runs write to REVIEW_REPAIR; legacy
    # runs wrote to REVIEW_FINAL → REPAIR_ITER_2 → ... → REVIEW_INITIAL
    # in priority order (latest first).
    _REVIEW_LOOKBACK_PHASES = (
        SubPhase.REVIEW_REPAIR,
        SubPhase.REVIEW_FINAL,
        SubPhase.REPAIR_ITER_2,
        SubPhase.REPAIR_ITER_1,
        SubPhase.REPAIR_ITER_0,
        SubPhase.REVIEW_INITIAL,
    )

    def _load_coverage_if_done(self, state: MilestoneState) -> Optional[CoverageReport]:
        for phase in self._REVIEW_LOOKBACK_PHASES:
            if _has(state, phase):
                art = state.read_artifact(phase) or {}
                cov = art.get("coverage")
                if cov:
                    try:
                        return CoverageReport.model_validate(cov)
                    except Exception:
                        return None
        return None

    def _load_review_if_done(self, state: MilestoneState) -> Optional[CodeReviewReport]:
        for phase in self._REVIEW_LOOKBACK_PHASES:
            if _has(state, phase):
                art = state.read_artifact(phase) or {}
                cr = art.get("code_review")
                if cr:
                    try:
                        return CodeReviewReport.model_validate(cr)
                    except Exception:
                        return None
        return None

    def _reload_required(self, state: MilestoneState, phase: SubPhase, cls):
        art = state.read_artifact(phase)
        if art is None:
            self._gates.hard(
                "missing_state_artifact",
                f"phase {phase.value} marked complete but no artifact on disk",
            )
        try:
            return cls.model_validate(art)
        except Exception as e:
            self._gates.hard(
                "corrupt_state_artifact",
                f"phase {phase.value} artifact failed schema validation: {e}",
            )

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    def _is_final_milestone(self, milestone: Milestone) -> bool:
        """True when this milestone is the last in the plan.

        The final milestone is always treated as a refactor boundary
        regardless of ``milestone.refactor_after``. Needs the loop's
        constructor-provided ``total_milestones`` count to decide;
        falls back to ``False`` if not known.
        """
        if self._total_milestones is None:
            return False
        # sequence_index is 0-based per planner/types.py
        return milestone.sequence_index >= (self._total_milestones - 1)


# ── Module-level helpers ────────────────────────────────────────────────


def _has(state: MilestoneState, phase: SubPhase) -> bool:
    return phase in state.completed_phases()


def _safe_read(workspace: BaseWorkspace, path: str) -> Optional[str]:
    try:
        p = workspace.path(path)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    return None


def _merge_to_repair_report(
    milestone_name: str,
    coverage: Optional[CoverageReport],
    code_review: Optional[CodeReviewReport],
) -> CodeReviewReport:
    """Merge coverage gaps (from QE.review) into a CodeReviewReport so
    the Engineer's repair() entry can consume both as one report.

    Coverage gaps become ``MissingErrorHandling`` entries (capability_id
    + scenario as the missing error). Source-code findings come from the
    CodeReviewReport directly. Approval is False if either was unapproved.
    """
    from bizniz.code_reviewer.types import (
        AntiPatternViolation, CodeReviewReport as CRR, FlaggedSymbol,
        MissingErrorHandling, UngatedAuthCapability,
    )
    flagged: List[FlaggedSymbol] = list(code_review.flagged_symbols) if code_review else []
    anti: List[AntiPatternViolation] = list(code_review.anti_pattern_violations) if code_review else []
    ungated: List[UngatedAuthCapability] = list(code_review.ungated_auth) if code_review else []
    missing: List[MissingErrorHandling] = list(code_review.missing_error_handling) if code_review else []
    recommendations: List[str] = list(code_review.recommendations) if code_review else []

    if coverage:
        for ms in coverage.missing_scenarios:
            missing.append(MissingErrorHandling(
                file="",
                capability_id=ms.capability_id,
                error_case=f"untested scenario ({ms.priority}): {ms.scenario}",
                severity="critical" if ms.priority == "critical" else "warning",
            ))
        for cap_id, verdict in coverage.coverage_by_capability.items():
            if verdict == "missing":
                missing.append(MissingErrorHandling(
                    file="",
                    capability_id=cap_id,
                    error_case="capability has no test coverage",
                    severity="critical",
                ))

    summary_bits = []
    if code_review:
        summary_bits.append(code_review.summary)
    if coverage:
        summary_bits.append(
            f"coverage {coverage.covered_count}/{coverage.total_count}; "
            f"{len(coverage.missing_scenarios)} gap(s)"
        )
    summary = " — ".join(b for b in summary_bits if b)

    return CRR(
        milestone_name=milestone_name,
        approved=False,  # repair is only invoked when not approved
        flagged_symbols=flagged,
        anti_pattern_violations=anti,
        ungated_auth=ungated,
        missing_error_handling=missing,
        recommendations=recommendations,
        summary=summary,
        confidence=min(
            code_review.confidence if code_review else 1.0,
            coverage.confidence if coverage else 1.0,
        ),
    )
