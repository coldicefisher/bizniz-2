"""``V4MilestoneCodeDispatcher`` — v4 pipeline IMPLEMENT + REPAIR phase.

Per the v4 spec (docs/architecture/v4_pipeline_spec.md), v4 dispatches
each issue independently via ``CoderTesterAgent`` (unified code+test
agent) + ``PerIssueValidator`` (deterministic gates + fix-loop), all
parallelized through ``PIRunner`` (DAG-aware ThreadPoolExecutor).

Differences vs ``V3MilestoneCodeDispatcher``:

  - v3: 1 ServicePlanner + 1 CoderAgent per service (per-milestone fill)
  - v4: 1 ServicePlanner per service, then N CoderTesterAgent dispatches
        in parallel (per-issue), each followed by a per-issue validator

Repair is symmetric: ServicePlanner.repair() emits fix-issues, and
PIRunner fans them out through the same CoderTesterAgent +
PerIssueValidator pipeline. Repair-tier list is Opus-only by default
(no Haiku→Opus escalation chain — repair is by definition the harder
case).

Same outer contract as v3 dispatcher: returns ``EngineerResult`` so
MilestoneLoop is unchanged downstream.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.coder.types import Issue as CoderIssue
from bizniz.coder_tester.agent import CoderTesterAgent
from bizniz.coder_tester.types import FilledFile as CtFilledFile
from bizniz.engineer.types import (
    EngineerPlan, EngineerResult, Issue as EngineerIssue,
)
from bizniz.lib.dependency_graph import topological_layers
from bizniz.orchestrator.parallel_issue_runner import (
    PIRunner, PIRunnerResult,
)
from bizniz.per_issue_validator.types import ValidatedIssue
from bizniz.per_issue_validator.validator import PerIssueValidator
from bizniz.quality_engineer.types import EnrichedSpec
from bizniz.service_planner.scaffolded import (
    ScaffoldedPlanResult, ServicePlannerWithScaffold,
)
from bizniz.state.issue_store import IssueStateStore
from bizniz.workspace.base_workspace import BaseWorkspace


ServicePlannerV4Factory = Callable[[ServiceDefinition], ServicePlannerWithScaffold]
CoderTesterFactory = Callable[[ServiceDefinition], CoderTesterAgent]
WorkspaceForService = Callable[[str], BaseWorkspace]


def _is_code_bearing(service: ServiceDefinition) -> bool:
    lang = (service.language or "").lower()
    return lang not in {"yaml", "sql"}


class V4MilestoneCodeDispatcher:
    """v4 IMPLEMENT + REPAIR dispatcher: per-issue parallel fan-out
    via PIRunner + CoderTesterAgent + PerIssueValidator.

    Same outer EngineerResult contract as v3.
    """

    def __init__(
        self,
        *,
        planner_factory: ServicePlannerV4Factory,
        coder_tester_factory: CoderTesterFactory,
        workspace_for_service: WorkspaceForService,
        max_parallel_coders: int = 6,
        repair_coder_tester_factory: Optional[CoderTesterFactory] = None,
        issue_store: Optional[IssueStateStore] = None,
        on_status: Optional[Callable[[str], None]] = None,
        only_service: Optional[str] = None,
        # repair_planner_factory is optional — used only by ``repair()``
        # to build fix-issues from coverage + code_review reports. v3.1
        # injects this from the v2 ServicePlanner (which has a repair()
        # method). When None, .repair() raises.
        repair_planner_factory: Optional[Callable[[ServiceDefinition], object]] = None,
    ):
        self._planner_factory = planner_factory
        self._coder_tester_factory = coder_tester_factory
        self._repair_coder_tester_factory = (
            repair_coder_tester_factory or coder_tester_factory
        )
        self._workspace_for_service = workspace_for_service
        self._max_parallel_coders = max(1, int(max_parallel_coders))
        self._issue_store = issue_store
        self._on_status = on_status
        self._only_service = only_service
        self._repair_planner_factory = repair_planner_factory

    # ── IMPLEMENT phase ────────────────────────────────────────────

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
        """Plan + parallel-dispatch each service. ``workspace_summary``
        is accepted for v3 compat but unused (the seeded scaffold
        carries the contract)."""
        self._log(
            f"V4MilestoneCodeDispatcher: starting "
            f"(max_parallel={self._max_parallel_coders})"
        )
        active_store = issue_store if issue_store is not None else self._issue_store

        layers = topological_layers(list(architecture.services))
        all_issues: List[EngineerIssue] = []
        completed: List[str] = []
        deferred: List[str] = []
        per_service_summaries: List[str] = []
        per_service_notes: List[str] = []

        for layer in layers:
            for service in layer:
                if not _is_code_bearing(service):
                    self._log(
                        f"V4MilestoneCodeDispatcher: skipping `{service.name}` "
                        f"(language='{service.language}' is infrastructure-only)"
                    )
                    continue
                if (self._only_service is not None
                        and service.name != self._only_service):
                    self._log(
                        f"V4MilestoneCodeDispatcher: skipping `{service.name}` "
                        f"(only_service={self._only_service!r})"
                    )
                    continue
                skeleton_md = (
                    skeleton_md_for_service(service.name)
                    if skeleton_md_for_service else None
                )

                result = self._dispatch_service(
                    service=service,
                    architecture=architecture,
                    enriched_spec=enriched_spec,
                    skeleton_md=skeleton_md,
                    auth_contract=auth_contract,
                    active_store=active_store,
                    repair_iteration=0,
                )
                completed.extend(result["completed"])
                deferred.extend(result["deferred"])
                for issue in result["issues"]:
                    all_issues.append(EngineerIssue(
                        id=issue.id,
                        title=issue.title,
                        description=issue.description,
                        target_files=list(issue.target_files),
                        test_files=list(issue.test_files),
                        success_criteria=list(issue.success_criteria),
                        depends_on=list(issue.depends_on),
                        spec_refs=list(issue.spec_refs),
                        status="done" if issue.id in result["completed"] else "blocked",
                    ))
                per_service_summaries.append(
                    f"`{service.name}`: {len(result['completed'])}/"
                    f"{len(result['issues'])} issues completed in "
                    f"{result['wall_s']:.1f}s"
                )
                per_service_notes.extend(result["notes"])

        plan = EngineerPlan(
            approach=(
                f"v4 IMPLEMENT (parallel CoderTester per issue, "
                f"max_parallel={self._max_parallel_coders}) on "
                f"{len(all_issues)} issue(s) across "
                f"{len(per_service_summaries)} service(s)."
            ),
            issues=all_issues,
        )

        if deferred:
            final_status = "partial"
        elif completed:
            final_status = "passed"
        else:
            final_status = "not_run"

        return EngineerResult(
            plan=plan,
            summary="\n".join(per_service_summaries),
            final_test_status=final_status,
            completed_issue_ids=list(completed),
            deferred_issue_ids=list(deferred),
            completed_units=list(completed),
            deferred_units=list(deferred),
            notes=per_service_notes,
        )

    # ── REPAIR phase ──────────────────────────────────────────────

    def repair(
        self,
        *,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        coverage_report,
        code_review_report,
        repair_iteration: int,
        auth_contract: Optional[str] = None,
        workspace_summary: Optional[str] = None,
        issue_store: Optional[IssueStateStore] = None,
        skeleton_md_for_service: Optional[Callable[[str], Optional[str]]] = None,
    ) -> EngineerResult:
        """Build repair issues from coverage + code-review findings,
        then dispatch them through PIRunner with the same per-issue
        pipeline IMPLEMENT uses (Opus-tier factory by default for
        repair)."""
        if self._repair_planner_factory is None:
            raise RuntimeError(
                "V4MilestoneCodeDispatcher.repair() called but no "
                "repair_planner_factory was injected. Pass one in "
                "the constructor."
            )

        self._log(
            f"V4MilestoneCodeDispatcher: REPAIR iter {repair_iteration} "
            f"(max_parallel={self._max_parallel_coders}, Opus tier)"
        )
        active_store = issue_store if issue_store is not None else self._issue_store

        all_issues: List[EngineerIssue] = []
        completed: List[str] = []
        deferred: List[str] = []
        per_service_summaries: List[str] = []
        per_service_notes: List[str] = []

        layers = topological_layers(list(architecture.services))
        for layer in layers:
            for service in layer:
                if not _is_code_bearing(service):
                    continue
                if (self._only_service is not None
                        and service.name != self._only_service):
                    continue
                skeleton_md = (
                    skeleton_md_for_service(service.name)
                    if skeleton_md_for_service else None
                )

                repair_planner = self._repair_planner_factory(service)
                self._log(
                    f"V4 repair: planning `{service.name}` "
                    f"(iter {repair_iteration})"
                )
                t0 = time.time()
                # Pull prior issues + dispositions from the issue store
                # when available (production ServicePlanner.plan_repair
                # uses them as context). Empty when no store wired.
                prior_issues: list = []
                prior_dispositions: dict = {}
                if active_store is not None:
                    try:
                        prior_issues = list(
                            active_store.list_issues_for_service(service.name) or []
                        )
                        prior_dispositions = dict(
                            active_store.dispositions_for_service(service.name) or {}
                        ) if hasattr(active_store, "dispositions_for_service") else {}
                    except Exception:
                        prior_issues = []
                        prior_dispositions = {}
                try:
                    # Production ServicePlanner uses ``plan_repair``; the
                    # scaffolded variant doesn't have a repair() method
                    # yet so we route through the production planner
                    # that v2_build wires into ``repair_planner_factory``.
                    if hasattr(repair_planner, "plan_repair"):
                        fix_issues_raw = repair_planner.plan_repair(
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
                    else:
                        # Fallback: object with a .repair() that returns
                        # something with .issues (scaffolded variant, if
                        # one ever lands).
                        result_obj = repair_planner.repair(
                            architecture=architecture,
                            enriched_spec=enriched_spec,
                            service=service,
                            coverage_report=coverage_report,
                            code_review_report=code_review_report,
                            repair_iteration=repair_iteration,
                            skeleton_md=skeleton_md,
                            auth_contract=auth_contract,
                        )
                        fix_issues_raw = list(
                            getattr(result_obj, "issues", []) or []
                        )
                except Exception as e:
                    self._log(
                        f"V4 repair: `{service.name}` planner failed: "
                        f"{type(e).__name__}: {e}"
                    )
                    per_service_notes.append(
                        f"`{service.name}` repair planner failed: "
                        f"{type(e).__name__}: {e}"
                    )
                    continue

                fix_issues = list(fix_issues_raw or [])
                seeded_files: list = []  # plan_repair doesn't emit seeds
                if not fix_issues:
                    self._log(
                        f"V4 repair: `{service.name}` planner emitted 0 "
                        f"fix-issues (nothing to repair?)"
                    )
                    continue

                self._log(
                    f"V4 repair: `{service.name}` → {len(fix_issues)} "
                    f"fix-issue(s) (took {time.time() - t0:.1f}s)"
                )

                # Materialize any new seeded scaffold from repair.
                self._materialize_seed(service, seeded_files)

                # Dispatch via PIRunner with repair tier factory.
                runner_result = self._run_pirunner(
                    service=service,
                    architecture=architecture,
                    enriched_spec=enriched_spec,
                    issues=fix_issues,
                    seeded_files=seeded_files,
                    skeleton_md=skeleton_md,
                    auth_contract=auth_contract,
                    use_repair_tier=True,
                )

                svc_completed, svc_deferred = self._summarize_run(
                    fix_issues, runner_result, active_store, service.name,
                )
                completed.extend(svc_completed)
                deferred.extend(svc_deferred)
                for issue in fix_issues:
                    all_issues.append(EngineerIssue(
                        id=issue.id,
                        title=issue.title,
                        description=issue.description,
                        target_files=list(issue.target_files),
                        test_files=list(issue.test_files),
                        success_criteria=list(issue.success_criteria),
                        depends_on=list(issue.depends_on),
                        spec_refs=list(issue.spec_refs),
                        status="done" if issue.id in svc_completed else "blocked",
                    ))
                per_service_summaries.append(
                    f"`{service.name}`: {len(svc_completed)}/"
                    f"{len(fix_issues)} fix-issues in "
                    f"{runner_result.wall_s:.1f}s"
                )

        plan = EngineerPlan(
            approach=(
                f"v4 REPAIR iter {repair_iteration} (parallel CoderTester "
                f"per fix-issue, max_parallel={self._max_parallel_coders}, "
                f"Opus tier) on {len(all_issues)} fix-issue(s)."
            ),
            issues=all_issues,
        )
        final_status = (
            "partial" if deferred else
            ("passed" if completed else "not_run")
        )
        return EngineerResult(
            plan=plan,
            summary="\n".join(per_service_summaries),
            final_test_status=final_status,
            completed_issue_ids=list(completed),
            deferred_issue_ids=list(deferred),
            completed_units=list(completed),
            deferred_units=list(deferred),
            notes=per_service_notes,
        )

    # ── Service dispatch ──────────────────────────────────────────

    def _dispatch_service(
        self,
        *,
        service: ServiceDefinition,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        skeleton_md: Optional[str],
        auth_contract: Optional[str],
        active_store: Optional[IssueStateStore],
        repair_iteration: int,
    ) -> dict:
        """IMPLEMENT-phase dispatch for one service."""
        t0 = time.time()
        planner = self._planner_factory(service)
        self._log(
            f"V4MilestoneCodeDispatcher: planning + seeding `{service.name}`"
        )
        try:
            plan_result = planner.plan_service(
                architecture=architecture,
                enriched_spec=enriched_spec,
                service=service,
                skeleton_md=skeleton_md,
                auth_contract=auth_contract,
            )
        except Exception as e:
            wall = time.time() - t0
            self._log(
                f"V4MilestoneCodeDispatcher: `{service.name}` planner "
                f"failed in {wall:.1f}s: {type(e).__name__}: {e}"
            )
            return {
                "issues": [], "completed": [], "deferred": [],
                "wall_s": wall,
                "notes": [f"planner failed: {type(e).__name__}: {e}"],
            }

        issues = plan_result.issues
        seeded_files = plan_result.seeded_files
        self._log(
            f"V4MilestoneCodeDispatcher: `{service.name}` planned "
            f"{len(issues)} issue(s), {len(seeded_files)} seeded file(s)"
        )

        if active_store is not None:
            try:
                active_store.record_planned(service.name, issues)
            except Exception as e:
                self._log(
                    f"V4MilestoneCodeDispatcher: record_planned failed "
                    f"(non-fatal): {type(e).__name__}: {e}"
                )

        self._materialize_seed(service, seeded_files)

        if active_store is not None:
            for issue in issues:
                try:
                    active_store.mark_started(
                        service.name, issue.id, "claude-cli:v4",
                    )
                except Exception:
                    pass

        runner_result = self._run_pirunner(
            service=service,
            architecture=architecture,
            enriched_spec=enriched_spec,
            issues=issues,
            seeded_files=seeded_files,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
            use_repair_tier=False,
        )

        completed, deferred = self._summarize_run(
            issues, runner_result, active_store, service.name,
        )
        wall = time.time() - t0
        return {
            "issues": issues,
            "completed": completed,
            "deferred": deferred,
            "wall_s": wall,
            "notes": [runner_result.summary_line()],
        }

    # ── PIRunner wiring ──────────────────────────────────────────

    def _run_pirunner(
        self,
        *,
        service: ServiceDefinition,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        issues: List[CoderIssue],
        seeded_files: list,
        skeleton_md: Optional[str],
        auth_contract: Optional[str],
        use_repair_tier: bool,
    ) -> PIRunnerResult:
        """Build the per-issue runner closure + dispatch through PIRunner."""
        agent_factory = (
            self._repair_coder_tester_factory if use_repair_tier
            else self._coder_tester_factory
        )
        agent = agent_factory(service)
        workspace = self._workspace_for_service(service.workspace_name)
        validator = PerIssueValidator(
            agent=agent,
            workspace=workspace,
            on_status=self._on_status,
        )

        # Pre-compute per-issue seeded file slices + sibling summaries
        # (one summary per OTHER issue, so the prompt stays bounded).
        seeded_by_path = {sf.path: sf for sf in seeded_files}
        sibling_summaries: List[str] = []
        for i in issues:
            sib_files = ", ".join((i.target_files or [])[:3])
            sibling_summaries.append(
                f"`{i.id}` — {i.title} ({sib_files})"
            )

        def per_issue_runner(issue: CoderIssue) -> ValidatedIssue:
            # Build the seeded scaffold for THIS issue only.
            issue_paths = set(issue.target_files) | set(issue.test_files)
            issue_seed = [
                CtFilledFile(
                    path=sf.path, content=sf.content, role="code",
                )
                for sf in seeded_files
                if sf.path in issue_paths
            ]
            # Filter sibling list to exclude THIS issue.
            siblings = [s for s in sibling_summaries if not s.startswith(f"`{issue.id}` ")]
            # Initial agent dispatch.
            initial = agent.code_issue(
                issue=issue,
                service=service,
                seeded_files=issue_seed,
                capabilities=list(enriched_spec.capabilities or []),
                skeleton_md=skeleton_md,
                auth_contract=auth_contract,
                sibling_issue_summaries=siblings,
            )
            # Validate + fix-loop.
            return validator.validate(
                issue=issue,
                initial_result=initial,
                service=service,
                capabilities=list(enriched_spec.capabilities or []),
                seeded_files=issue_seed,
                skeleton_md=skeleton_md,
                auth_contract=auth_contract,
                sibling_issue_summaries=siblings,
            )

        runner = PIRunner(
            max_parallel=self._max_parallel_coders,
            on_status=self._on_status,
        )
        return runner.run(issues=issues, issue_runner=per_issue_runner)

    # ── Helpers ──────────────────────────────────────────────────

    def _materialize_seed(self, service, seeded_files) -> None:
        if not seeded_files:
            return
        try:
            workspace = self._workspace_for_service(service.workspace_name)
            ws_root = getattr(workspace, "root", None)
            if ws_root is None:
                return
            ws_path = Path(str(ws_root))
            ws_path.mkdir(parents=True, exist_ok=True)
            for sf in seeded_files:
                dest = ws_path / sf.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(sf.content, encoding="utf-8")
        except Exception as e:
            self._log(
                f"V4MilestoneCodeDispatcher: seed materialization "
                f"warning (continuing): {type(e).__name__}: {e}"
            )

    def _summarize_run(
        self,
        issues,
        runner_result: PIRunnerResult,
        active_store,
        service_name: str,
    ):
        completed: List[str] = []
        deferred: List[str] = []
        by_id = {v.issue_id: v for v in runner_result.validated}
        for issue in issues:
            v = by_id.get(issue.id)
            if v is None:
                deferred.append(issue.id)
                continue
            if v.clean:
                completed.append(issue.id)
                if active_store is not None:
                    try:
                        active_store.mark_finished(
                            service_name, issue.id, status="passed",
                        )
                    except Exception:
                        pass
            else:
                deferred.append(issue.id)
                if active_store is not None:
                    try:
                        active_store.mark_finished(
                            service_name, issue.id,
                            status="errored",
                            error=v.halt_reason or "validation_failed",
                        )
                    except Exception:
                        pass
        return completed, deferred

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass
