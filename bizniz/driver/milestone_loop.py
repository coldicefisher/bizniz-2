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
from bizniz.driver.state import MilestoneState, SubPhase, next_subphase
from bizniz.engineer.agent import Engineer
from bizniz.engineer.types import EngineerResult
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
        gates: GatePolicy,
        workspace_for_service: Callable[[str], BaseWorkspace],
        primary_workspace: BaseWorkspace,
        compose_path: str,
        project_root: Path,
        repair_budget: int = 3,
        repair_engineer_factory: Optional[Callable[[int], Engineer]] = None,
        cost_tracker=None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._engineer = engineer
        self._qe = quality_engineer
        self._cr = code_reviewer
        self._integration = integration_phase
        self._gates = gates
        self._workspace_for_service = workspace_for_service
        self._primary_workspace = primary_workspace
        self._compose_path = compose_path
        self._project_root = project_root
        self._repair_budget = max(0, min(3, repair_budget))
        self._repair_engineer_factory = repair_engineer_factory
        self._cost_tracker = cost_tracker
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

    # ── Public ─────────────────────────────────────────────────────────

    def run(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        prior_specs: Iterable[EnrichedSpec],
        auth_contract: Optional[str],
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
        prior_list = list(prior_specs or [])

        # Phase artifacts assembled in-memory as the loop progresses.
        spec: Optional[EnrichedSpec] = self._load_spec_if_done(state)
        result: Optional[EngineerResult] = self._load_engineer_result_if_done(state)
        coverage: Optional[CoverageReport] = self._load_coverage_if_done(state)
        code_review: Optional[CodeReviewReport] = self._load_review_if_done(state)
        repair_iterations = 0
        integration_api: Optional[IntegrationPhaseResult] = None
        integration_web: Optional[IntegrationPhaseResult] = None

        # Walk the phases in order; each branch skips if already done.

        if not _has(state, SubPhase.ENRICH):
            self._tag(state.milestone_index, SubPhase.ENRICH)
            spec = self._phase_enrich(milestone, architecture, auth_contract, prior_list)
            state.mark_phase(SubPhase.ENRICH, spec)
        spec = spec or self._reload_required(state, SubPhase.ENRICH, EnrichedSpec)

        if not _has(state, SubPhase.IMPLEMENT):
            self._tag(state.milestone_index, SubPhase.IMPLEMENT)
            result = self._phase_implement(milestone, architecture, spec, auth_contract, prior_list)
            state.mark_phase(SubPhase.IMPLEMENT, result)
        result = result or self._reload_required(state, SubPhase.IMPLEMENT, EngineerResult)

        # Reviews + repairs.
        repair_phases = (
            SubPhase.REVIEW_INITIAL,
            SubPhase.REPAIR_ITER_0,
            SubPhase.REPAIR_ITER_1,
            SubPhase.REPAIR_ITER_2,
            SubPhase.REVIEW_FINAL,
        )
        # Determine where to start in the repair sequence.
        for idx, phase in enumerate(repair_phases):
            if _has(state, phase):
                continue
            if phase == SubPhase.REVIEW_INITIAL:
                self._tag(state.milestone_index, phase)
                coverage, code_review = self._phase_review(
                    milestone, architecture, spec, result, auth_contract, prior_list,
                )
                state.mark_phase(phase, {
                    "coverage": coverage.model_dump(),
                    "code_review": code_review.model_dump(),
                })
                if self._approved(coverage, code_review):
                    # Skip ahead — mark final review as done with same artifact.
                    state.mark_phase(SubPhase.REVIEW_FINAL, {
                        "coverage": coverage.model_dump(),
                        "code_review": code_review.model_dump(),
                    })
                    break
                continue

            if phase == SubPhase.REVIEW_FINAL:
                # Reached terminal review with no remaining repairs (budget
                # depleted or exited early). Last-known coverage/review
                # decide approval.
                if not self._approved(coverage, code_review):
                    self._gates.hard(
                        "milestone_unapproved",
                        f"M{state.milestone_index} '{milestone.name}' not "
                        f"approved after {repair_iterations} repair iteration(s). "
                        f"coverage.approved={coverage and coverage.approved}, "
                        f"code_review.approved={code_review and code_review.approved}",
                    )
                state.mark_phase(phase, {
                    "coverage": coverage.model_dump() if coverage else {},
                    "code_review": code_review.model_dump() if code_review else {},
                })
                break

            # Repair iteration phase.
            iter_idx = idx - 1  # REPAIR_ITER_0 is at idx=1, etc.
            if iter_idx >= self._repair_budget:
                # Budget exhausted; jump to final review/halt.
                continue
            if self._approved(coverage, code_review):
                # Already passed; skip remaining repair phases.
                continue
            self._log(
                f"MilestoneLoop: repair iteration {iter_idx} "
                f"(model escalation tier {iter_idx})"
            )
            self._tag(state.milestone_index, phase)
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
            repair_iterations += 1
            coverage, code_review = self._phase_review(
                milestone, architecture, spec, result, auth_contract, prior_list,
            )
            state.mark_phase(phase, {
                "engineer_result": result.model_dump(),
                "coverage": coverage.model_dump(),
                "code_review": code_review.model_dump(),
            })
            if self._approved(coverage, code_review):
                # Skip remaining repair phases; mark REVIEW_FINAL.
                state.mark_phase(SubPhase.REVIEW_FINAL, {
                    "coverage": coverage.model_dump(),
                    "code_review": code_review.model_dump(),
                })
                break

        # Integration phases.
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
            integration_web=integration_web.model_dump() if integration_web else None,
        )

    # ── Phase implementations ───────────────────────────────────────────

    def _phase_enrich(
        self, milestone, architecture, auth_contract, prior_list,
    ) -> EnrichedSpec:
        return self._qe.enrich(
            milestone=milestone,
            architecture=architecture,
            auth_contract=auth_contract,
            prior_specs=prior_list,
        )

    def _phase_implement(
        self, milestone, architecture, spec, auth_contract, prior_list,
    ) -> EngineerResult:
        return self._engineer.implement(
            milestone=milestone,
            architecture=architecture,
            enriched_spec=spec,
            auth_contract=auth_contract,
            prior_specs=prior_list,
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
        if not _has(state, SubPhase.IMPLEMENT):
            return None
        return self._reload_required(state, SubPhase.IMPLEMENT, EngineerResult)

    def _load_coverage_if_done(self, state: MilestoneState) -> Optional[CoverageReport]:
        # Look for the most recent review artifact (final → repair → initial)
        for phase in (SubPhase.REVIEW_FINAL, SubPhase.REPAIR_ITER_2,
                      SubPhase.REPAIR_ITER_1, SubPhase.REPAIR_ITER_0,
                      SubPhase.REVIEW_INITIAL):
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
        for phase in (SubPhase.REVIEW_FINAL, SubPhase.REPAIR_ITER_2,
                      SubPhase.REPAIR_ITER_1, SubPhase.REPAIR_ITER_0,
                      SubPhase.REVIEW_INITIAL):
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
