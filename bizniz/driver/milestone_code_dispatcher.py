"""v2.5 milestone-level code dispatcher.

Replaces the v2 Engineer's ``implement()`` step. Coordinates:
  1. ServicePlanner per service → List[Coder Issue]
  2. Orchestrator per service → drives Coder issue-by-issue with
     model escalation
  3. Aggregates into an ``EngineerResult``-shaped object so the rest
     of MilestoneLoop (QE.review, CodeReviewer.review, repair, integration)
     keeps working unchanged.

Why an adapter and not a wholesale MilestoneLoop rewrite: the existing
review/repair/integration phases all read EngineerResult fields. Until
v2.5's full review-and-repair path is built, this adapter is the
smallest seam that lets us run the new code path inside the existing
pipeline.

Key v2 ↔ v2.5 mapping:
- v2 ``Issue`` (single milestone-wide list) ← flatten across services
- v2 ``EngineerPlan.approach`` ← computed summary
- v2 ``IssueStatus`` "done"|"blocked"|"skipped" ← from
  ``IssueOutcome.disposition``
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.coder.agent import Coder
from bizniz.coder.types import Issue as CoderIssue
from bizniz.engineer.types import (
    EngineerPlan, EngineerResult, Issue as EngineerIssue,
)
from bizniz.lib.dependency_graph import topological_layers
from bizniz.lib.model_progression import ModelProgression
from bizniz.orchestrator.orchestrator import Orchestrator
from bizniz.orchestrator.types import IssueOutcome, OrchestratorResult
from bizniz.quality_engineer.types import CoverageReport, EnrichedSpec
from bizniz.service_planner.agent import ServicePlanner
from bizniz.state.issue_store import IssueStateStore


# Languages the Coder can actually generate + test code for. Services
# whose language isn't in this set are pure infrastructure (postgres
# init.sql, redis.conf, fusionauth kickstart) — they're materialized
# by the Provisioner and don't need ServicePlanner / Coder. Trying to
# plan them just burns API spend on issues the pytest sidecar can't
# possibly green-test.
_CODE_BEARING_LANGUAGES = {"python", "typescript", "javascript", "ts", "js", "py"}


def _is_code_bearing(service: ServiceDefinition) -> bool:
    return (service.language or "").lower() in _CODE_BEARING_LANGUAGES


CoderFactory = Callable[[str, ServiceDefinition], Coder]
"""(model_name, service) → Coder bound to that service's workspace + model."""

ServicePlannerFactory = Callable[[ServiceDefinition], ServicePlanner]
"""service → ServicePlanner. Lets the caller bind a per-service client
or escalation, though most callers will use the same client for all
services."""

ProgressionFactory = Callable[[ServiceDefinition], ModelProgression]
"""service → fresh ModelProgression. New per service so escalation in
one service doesn't bleed into another."""


class MilestoneCodeDispatcher:
    """Drives all services for one milestone via the v2.5 trio."""

    def __init__(
        self,
        service_planner_factory: ServicePlannerFactory,
        coder_factory: CoderFactory,
        progression_factory: ProgressionFactory,
        issue_store: Optional[IssueStateStore] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._planner_factory = service_planner_factory
        self._coder_factory = coder_factory
        self._progression_factory = progression_factory
        self._issue_store = issue_store
        self._on_status = on_status

    def run(
        self,
        *,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        auth_contract: Optional[str] = None,
        skeleton_md_for_service: Optional[Callable[[str], Optional[str]]] = None,
        workspace_summary: Optional[str] = None,
        issue_store: Optional[IssueStateStore] = None,
    ) -> EngineerResult:
        """Plan + dispatch every service in topo order, return EngineerResult.

        Services are iterated in topological order based on their
        ``depends_on`` so a downstream service (e.g. backend) sees its
        upstream's code on disk before its Coder runs. Within a service,
        the Orchestrator handles per-issue topo + escalation.
        """
        self._log("MilestoneCodeDispatcher: starting")

        # Per-call store overrides the constructor-set one. MilestoneLoop
        # passes a per-milestone store; tests / one-off use can rely on
        # the constructor-set instance.
        active_store = issue_store if issue_store is not None else self._issue_store

        layers = topological_layers(list(architecture.services))
        all_issues: List[EngineerIssue] = []
        completed_ids: List[str] = []
        deferred_ids: List[str] = []
        per_service: List[OrchestratorResult] = []

        for layer in layers:
            for service in layer:
                if not _is_code_bearing(service):
                    self._log(
                        f"MilestoneCodeDispatcher: skipping `{service.name}` "
                        f"(language='{service.language}' is "
                        f"infrastructure-only — provisioner already "
                        f"materialized it; nothing to code)"
                    )
                    continue
                self._log(
                    f"MilestoneCodeDispatcher: planning service "
                    f"`{service.name}` ({service.framework}/{service.language})"
                )
                planner = self._planner_factory(service)
                skeleton_md = (
                    skeleton_md_for_service(service.name)
                    if skeleton_md_for_service else None
                )
                issues = planner.plan_service(
                    architecture=architecture,
                    enriched_spec=enriched_spec,
                    service=service,
                    skeleton_md=skeleton_md,
                    auth_contract=auth_contract,
                )
                self._log(
                    f"MilestoneCodeDispatcher: `{service.name}` planner "
                    f"emitted {len(issues)} issues"
                )

                # Persist planned issues immediately so resume sees them
                # even if the dispatcher dies mid-run.
                if active_store is not None:
                    active_store.record_planned(service.name, issues)

                progression = self._progression_factory(service)

                def make_coder(model: str, _service=service) -> Coder:
                    return self._coder_factory(model, _service)

                orchestrator = Orchestrator(
                    service=service.name,
                    coder_factory=make_coder,
                    progression=progression,
                    issue_store=active_store,
                    on_status=self._on_status,
                )
                outcome = orchestrator.run_service(
                    issues=issues,
                    architecture=architecture,
                    enriched_spec=enriched_spec,
                    auth_contract=auth_contract,
                    workspace_summary=workspace_summary,
                    skeleton_md=skeleton_md,
                )
                per_service.append(outcome)

                # Convert this service's results into v2 Issue shape and
                # roll up completed/deferred ids.
                for coder_issue in issues:
                    matching = next(
                        (o for o in outcome.issues
                         if o.issue_id == coder_issue.id),
                        None,
                    )
                    eng_issue = _to_engineer_issue(coder_issue, matching)
                    all_issues.append(eng_issue)
                    if matching and matching.passed:
                        completed_ids.append(eng_issue.id)
                    else:
                        deferred_ids.append(eng_issue.id)

        approach = self._build_approach(per_service)
        final_status = _final_test_status(per_service)

        self._log(
            f"MilestoneCodeDispatcher: done — {len(completed_ids)} completed, "
            f"{len(deferred_ids)} deferred, final={final_status}"
        )

        return EngineerResult(
            plan=EngineerPlan(approach=approach, issues=all_issues),
            summary=_build_summary(per_service),
            final_test_status=final_status,
            completed_issue_ids=completed_ids,
            deferred_issue_ids=deferred_ids,
            notes=_collect_notes(per_service),
        )

    # ── Repair ─────────────────────────────────────────────────────────

    def repair(
        self,
        *,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        coverage_report: Optional[CoverageReport],
        code_review_report: Optional[CodeReviewReport],
        repair_iteration: int,
        auth_contract: Optional[str] = None,
        skeleton_md_for_service: Optional[Callable[[str], Optional[str]]] = None,
        workspace_summary: Optional[str] = None,
        issue_store: Optional[IssueStateStore] = None,
    ) -> EngineerResult:
        """Run a repair iteration.

        Reads prior issues + dispositions from the store, asks
        ServicePlanner to emit fix-issues per service, dispatches them
        through the Orchestrator. Returns an EngineerResult assembled
        from the store (which now includes both original issues and
        fix-issues from this iteration).
        """
        self._log(
            f"MilestoneCodeDispatcher.repair: iter {repair_iteration} starting"
        )
        active_store = issue_store if issue_store is not None else self._issue_store
        if active_store is None:
            raise RuntimeError(
                "MilestoneCodeDispatcher.repair requires an IssueStateStore "
                "(prior issues + dispositions are read from it)."
            )

        layers = topological_layers(list(architecture.services))

        for layer in layers:
            for service in layer:
                if not _is_code_bearing(service):
                    continue
                # Pull this service's prior issues + dispositions from
                # the store (these are the originals + any prior repair
                # iterations).
                prior_rows = active_store.all_rows(service=service.name)
                if not prior_rows:
                    self._log(
                        f"repair: skipping `{service.name}` — no prior "
                        f"issues recorded; nothing to fix"
                    )
                    continue

                prior_issues = [
                    _row_to_coder_issue(r) for r in prior_rows
                ]
                prior_dispositions = {
                    r["issue_id"]: r["status"] for r in prior_rows
                }

                self._log(
                    f"repair: planning `{service.name}` "
                    f"({len(prior_issues)} prior issues)"
                )
                planner = self._planner_factory(service)
                skeleton_md = (
                    skeleton_md_for_service(service.name)
                    if skeleton_md_for_service else None
                )
                fix_issues = planner.plan_repair(
                    architecture=architecture,
                    enriched_spec=enriched_spec,
                    service=service,
                    prior_issues=prior_issues,
                    prior_dispositions=prior_dispositions,
                    coverage_report=coverage_report,
                    code_review_report=code_review_report,
                    repair_iteration=repair_iteration,
                    skeleton_md=skeleton_md,
                    auth_contract=auth_contract,
                )
                if not fix_issues:
                    self._log(
                        f"repair: `{service.name}` — planner emitted no "
                        f"fix-issues, skipping"
                    )
                    continue

                self._log(
                    f"repair: `{service.name}` → {len(fix_issues)} fix-issue(s)"
                )

                active_store.record_planned(service.name, fix_issues)

                progression = self._progression_factory(service)

                def make_coder(model: str, _service=service) -> Coder:
                    return self._coder_factory(model, _service)

                orchestrator = Orchestrator(
                    service=service.name,
                    coder_factory=make_coder,
                    progression=progression,
                    issue_store=active_store,
                    on_status=self._on_status,
                )
                orchestrator.run_service(
                    issues=fix_issues,
                    architecture=architecture,
                    enriched_spec=enriched_spec,
                    auth_contract=auth_contract,
                    workspace_summary=workspace_summary,
                    skeleton_md=skeleton_md,
                )

        # Reassemble the EngineerResult from the store — now reflects
        # the union of original outcomes + fix-issue outcomes.
        result = active_store.assemble_engineer_result()
        self._log(
            f"MilestoneCodeDispatcher.repair: iter {repair_iteration} done — "
            f"{len(result.completed_issue_ids)} passing, "
            f"{len(result.deferred_issue_ids)} not"
        )
        return result

    # ── Helpers ────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def _build_approach(self, per_service: List[OrchestratorResult]) -> str:
        if not per_service:
            return "No services to dispatch."
        bits: List[str] = []
        for r in per_service:
            issue_count = len(r.issues)
            passed = r.passed_count
            bits.append(
                f"{r.service}: {passed}/{issue_count} issues "
                f"({'all green' if r.all_passed else 'some pending'})"
            )
        return "; ".join(bits)


def _row_to_coder_issue(row) -> CoderIssue:
    """Hydrate a CoderIssue from a coder_issues row (for repair planning)."""
    import json as _json
    return CoderIssue(
        id=row["issue_id"],
        title=row["title"],
        description=row["description"],
        service=row["service"],
        language=row["language"] or "python",
        target_files=_json.loads(row["target_files"] or "[]"),
        test_files=_json.loads(row["test_files"] or "[]"),
        success_criteria=_json.loads(row["success_criteria"] or "[]"),
        spec_refs=_json.loads(row["spec_refs"] or "[]"),
        depends_on=_json.loads(row["depends_on"] or "[]"),
    )


def _to_engineer_issue(
    coder_issue: CoderIssue,
    outcome: Optional[IssueOutcome],
) -> EngineerIssue:
    """Project a v2.5 Coder Issue + its outcome into a v2 EngineerIssue."""
    if outcome is None:
        status = "pending"
    elif outcome.disposition in ("passed", "escalated"):
        status = "done"
    elif outcome.disposition == "skipped":
        status = "skipped"
    elif outcome.disposition == "stalled":
        status = "blocked"
    elif outcome.disposition == "errored":
        status = "blocked"
    elif outcome.disposition == "partial":
        status = "in_progress"
    else:
        status = "pending"

    return EngineerIssue(
        id=coder_issue.id,
        title=coder_issue.title,
        description=coder_issue.description,
        target_files=list(coder_issue.target_files),
        test_files=list(coder_issue.test_files),
        success_criteria=list(coder_issue.success_criteria),
        spec_refs=list(coder_issue.spec_refs),
        depends_on=list(coder_issue.depends_on),
        status=status,
    )


def _final_test_status(per_service: List[OrchestratorResult]) -> str:
    """Map the aggregate result to v2's final_test_status enum.

    - 'passed': every issue across every service passed/escalated
    - 'partial': some issues passed, some did not
    - 'failed': zero issues passed
    - 'not_run': no issues at all (empty plan)
    """
    if not per_service:
        return "not_run"
    total = sum(len(r.issues) for r in per_service)
    if total == 0:
        return "not_run"
    passed = sum(r.passed_count for r in per_service)
    if passed == total:
        return "passed"
    if passed == 0:
        return "failed"
    return "partial"


def _build_summary(per_service: List[OrchestratorResult]) -> str:
    if not per_service:
        return ""
    bits: List[str] = []
    for r in per_service:
        bits.append(
            f"{r.service}: {r.passed_count}/{len(r.issues)} issues passed"
        )
    return " · ".join(bits)


def _collect_notes(per_service: List[OrchestratorResult]) -> List[str]:
    notes: List[str] = []
    for r in per_service:
        for o in r.issues:
            if o.passed:
                continue
            tier_str = " → ".join(o.tiers_used) if o.tiers_used else "none"
            note = f"[{r.service}] {o.issue_id} {o.disposition}; tiers: {tier_str}"
            if o.error:
                note += f" — {o.error[:120]}"
            notes.append(note)
    return notes
