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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from bizniz.per_issue_validator.debugger import PerIssueDebugger
from bizniz.per_issue_validator.types import ValidatedIssue
from bizniz.per_issue_validator.validator import PerIssueValidator
from bizniz.workspace_context.builder import WorkspaceContextBuilder
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


def _read_workspace_file(workspace, path: str) -> Optional[str]:
    """Read a file from the workspace if it exists. Returns None on
    miss or error. Used by repair-mode dispatch to seed the
    CoderTesterAgent with what's currently on disk (after prior
    fix-issues' writes), not the planner's frozen output."""
    try:
        p = workspace.path(path)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    return None


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
        repair_planner_factory: Optional[Callable[[ServiceDefinition], object]] = None,
        compose_path: Optional[str] = None,
        # v4 Option 3 (2026-05-19): when True, PerIssueValidator
        # escalates a stalled structured fix-loop to PerIssueDebugger
        # (tool-loop with Edit/Write/Read/Bash, sequential, context-
        # truncating). Default True — the whole point of Option 3.
        enable_per_issue_debugger: bool = True,
        # Wall budget per debugger invocation. Bumped 600 → 3000
        # (2026-05-19 evening) after recipe_v4_v8 saw BA-fix1-1 hit
        # the 10-min timeout mid-investigation. Deep cases routinely
        # need 30+ min of tool-loop iteration; 50 min is a safety net
        # not a target.
        debugger_timeout_seconds: int = 3000,
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
        self._compose_path = compose_path
        self._enable_per_issue_debugger = bool(enable_per_issue_debugger)
        self._debugger_timeout_seconds = int(debugger_timeout_seconds)

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
            # v4 fix #2 (2026-05-19): parallelize services WITHIN a
            # topological layer. Services in the same layer don't
            # depend on each other (by definition of the layer) so
            # backend + frontend can run their planner + IMPLEMENT
            # concurrently. recipe_v4_v8 wasted ~5 min running them
            # sequentially.
            eligible = []
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
                eligible.append(service)

            if not eligible:
                continue

            self._log(
                f"V4MilestoneCodeDispatcher: dispatching {len(eligible)} "
                f"service(s) in parallel: {[s.name for s in eligible]}"
            )

            results_by_name: Dict[str, dict] = {}
            with ThreadPoolExecutor(max_workers=len(eligible)) as ex:
                future_to_name = {}
                for service in eligible:
                    skeleton_md = (
                        skeleton_md_for_service(service.name)
                        if skeleton_md_for_service else None
                    )
                    fut = ex.submit(
                        self._dispatch_service,
                        service=service,
                        architecture=architecture,
                        enriched_spec=enriched_spec,
                        skeleton_md=skeleton_md,
                        auth_contract=auth_contract,
                        active_store=active_store,
                        repair_iteration=0,
                    )
                    future_to_name[fut] = service.name
                for fut in as_completed(future_to_name):
                    name = future_to_name[fut]
                    try:
                        results_by_name[name] = fut.result()
                    except Exception as e:
                        self._log(
                            f"V4MilestoneCodeDispatcher: `{name}` raised "
                            f"{type(e).__name__}: {e}"
                        )
                        results_by_name[name] = {
                            "issues": [], "completed": [], "deferred": [],
                            "wall_s": 0.0,
                            "notes": [f"service raised: {type(e).__name__}: {e}"],
                        }

            # Aggregate in deterministic order (by service definition order).
            for service in eligible:
                result = results_by_name.get(service.name) or {
                    "issues": [], "completed": [], "deferred": [],
                    "wall_s": 0.0, "notes": [],
                }
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
            eligible = [
                s for s in layer
                if _is_code_bearing(s) and (
                    self._only_service is None or s.name == self._only_service
                )
            ]
            if not eligible:
                continue

            self._log(
                f"V4 repair: dispatching {len(eligible)} service(s) "
                f"in parallel: {[s.name for s in eligible]}"
            )

            # v4 fix #2 (2026-05-19): parallelize repair across
            # services within a layer (same as run()).
            results_by_name: Dict[str, dict] = {}
            with ThreadPoolExecutor(max_workers=len(eligible)) as ex:
                future_to_name = {}
                for service in eligible:
                    skeleton_md = (
                        skeleton_md_for_service(service.name)
                        if skeleton_md_for_service else None
                    )
                    fut = ex.submit(
                        self._repair_one_service,
                        service=service,
                        architecture=architecture,
                        enriched_spec=enriched_spec,
                        coverage_report=coverage_report,
                        code_review_report=code_review_report,
                        repair_iteration=repair_iteration,
                        skeleton_md=skeleton_md,
                        auth_contract=auth_contract,
                        active_store=active_store,
                        workspace_summary=workspace_summary,
                    )
                    future_to_name[fut] = service.name
                for fut in as_completed(future_to_name):
                    name = future_to_name[fut]
                    try:
                        results_by_name[name] = fut.result()
                    except Exception as e:
                        self._log(
                            f"V4 repair: `{name}` raised "
                            f"{type(e).__name__}: {e}"
                        )
                        results_by_name[name] = {
                            "issues": [], "completed": [], "deferred": [],
                            "wall_s": 0.0,
                            "notes": [f"raised: {type(e).__name__}: {e}"],
                        }

            for service in eligible:
                result = results_by_name.get(service.name) or {
                    "issues": [], "completed": [], "deferred": [],
                    "wall_s": 0.0, "notes": [],
                }
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
                    f"{len(result['issues'])} fix-issues in "
                    f"{result['wall_s']:.1f}s"
                )
                per_service_notes.extend(result["notes"])

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

    # ── Workspace summary (v4 fix #4) ─────────────────────────────

    def _compute_workspace_summary(
        self, service: ServiceDefinition,
    ) -> Optional[str]:
        """Render a compact summary of the service's workspace state
        for the repair planner. Lists modified files + their sizes
        + (when available) git status. Best-effort — returns None on
        any error so the prompt falls back to its v3.1 behavior.

        Capped at ~3000 chars so it doesn't blow up the planner
        prompt.
        """
        try:
            workspace = self._workspace_for_service(service.workspace_name)
            ws_root = getattr(workspace, "root", None)
            if ws_root is None:
                return None
            ws_path = Path(str(ws_root))
            if not ws_path.exists():
                return None
        except Exception:
            return None

        lines: List[str] = []
        try:
            import subprocess
            # git status — short form — within the workspace dir.
            proc = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(ws_path),
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                lines.append("### git status (short)")
                lines.append("```")
                lines.append(proc.stdout.strip())
                lines.append("```")
        except Exception:
            pass

        # Top-level file listing with sizes (skip __pycache__, .git).
        try:
            file_lines = []
            for p in sorted(ws_path.rglob("*.py")):
                if any(part in {"__pycache__", ".git", ".pytest_cache",
                                "node_modules", ".venv"} for part in p.parts):
                    continue
                try:
                    size = p.stat().st_size
                    rel = p.relative_to(ws_path)
                    file_lines.append(f"  {rel} ({size} bytes)")
                except Exception:
                    pass
            if file_lines:
                lines.append("\n### Python files on disk")
                lines.extend(file_lines[:60])  # cap to keep prompt sane
        except Exception:
            pass

        if not lines:
            return None
        summary = "\n".join(lines)
        if len(summary) > 3000:
            summary = summary[:3000] + "\n...(truncated)"
        return summary

    # ── Repair: per-service helper ────────────────────────────────

    def _repair_one_service(
        self,
        *,
        service: ServiceDefinition,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        coverage_report,
        code_review_report,
        repair_iteration: int,
        skeleton_md: Optional[str],
        auth_contract: Optional[str],
        active_store: Optional[IssueStateStore],
        workspace_summary: Optional[str] = None,
    ) -> dict:
        """One service's REPAIR pass. Same dict shape as
        ``_dispatch_service`` (issues, completed, deferred, wall_s,
        notes) so the caller can aggregate uniformly. v4 fix #2
        (2026-05-19): callable concurrently against sibling services."""
        t0 = time.time()
        repair_planner = self._repair_planner_factory(service)
        self._log(
            f"V4 repair: planning `{service.name}` "
            f"(iter {repair_iteration})"
        )

        # v4 fix #4: compute live workspace summary if caller didn't
        # provide one. The planner gets visibility into what's
        # currently on disk so it doesn't re-attempt already-done work.
        if not workspace_summary:
            workspace_summary = self._compute_workspace_summary(service)

        # Prior issues + dispositions context.
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
            if hasattr(repair_planner, "plan_repair"):
                # v4 fix #4 (2026-05-19): pass workspace_summary when
                # the planner accepts it. Production ServicePlanner.
                # plan_repair now does. If the call raises TypeError
                # because of an unexpected kwarg (older custom
                # planner), retry without workspace_summary.
                kw = dict(
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
                if workspace_summary:
                    kw["workspace_summary"] = workspace_summary
                try:
                    fix_issues_raw = repair_planner.plan_repair(**kw)
                except TypeError as e:
                    if workspace_summary and "workspace_summary" in str(e):
                        # Older planner — drop the new kwarg and retry.
                        kw.pop("workspace_summary", None)
                        fix_issues_raw = repair_planner.plan_repair(**kw)
                    else:
                        raise
            else:
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
            return {
                "issues": [], "completed": [], "deferred": [],
                "wall_s": time.time() - t0,
                "notes": [
                    f"`{service.name}` repair planner failed: "
                    f"{type(e).__name__}: {e}"
                ],
            }

        fix_issues = list(fix_issues_raw or [])
        seeded_files: list = []  # plan_repair doesn't emit seeds
        if not fix_issues:
            self._log(
                f"V4 repair: `{service.name}` planner emitted 0 "
                f"fix-issues (nothing to repair?)"
            )
            return {
                "issues": [], "completed": [], "deferred": [],
                "wall_s": time.time() - t0, "notes": [],
            }

        self._log(
            f"V4 repair: `{service.name}` → {len(fix_issues)} "
            f"fix-issue(s) (took {time.time() - t0:.1f}s)"
        )

        self._materialize_seed(service, seeded_files)

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
        return {
            "issues": fix_issues,
            "completed": svc_completed,
            "deferred": svc_deferred,
            "wall_s": time.time() - t0,
            "notes": [runner_result.summary_line()],
        }

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
        debugger: Optional[PerIssueDebugger] = None
        if self._enable_per_issue_debugger:
            try:
                debugger = PerIssueDebugger(
                    workspace=workspace,
                    compose_path=self._compose_path,
                    service_name=service.name,
                    timeout_seconds=self._debugger_timeout_seconds,
                    on_status=self._on_status,
                )
            except Exception as e:
                self._log(
                    f"V4MilestoneCodeDispatcher: per-issue debugger init "
                    f"failed ({type(e).__name__}: {e}) — running without "
                    f"escalation"
                )
                debugger = None

        # pytest_collect needs containers RUNNING. Smoke phase brings
        # them up AFTER IMPLEMENT — so at IMPLEMENT time, exec'ing
        # ``docker compose exec backend pytest`` fails immediately and
        # the agent burns iters chasing phantom env errors. Only pass
        # compose/service to the validator when we're in REPAIR (containers
        # are up by then). v5 hotfix 2026-05-20.
        validator_compose = self._compose_path if use_repair_tier else None
        validator_service = service.name if use_repair_tier else None
        validator = PerIssueValidator(
            agent=agent,
            workspace=workspace,
            on_status=self._on_status,
            compose_path=validator_compose,
            service_name=validator_service,
            debugger=debugger,
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

        # CTX-1 (2026-05-20): build the preventive context once per
        # service (workspace is stable between issues; per-issue
        # content varies). We pass the rendered section text to
        # each agent call.
        context_builder = WorkspaceContextBuilder(
            workspace=workspace, on_status=self._on_status,
        )

        def per_issue_runner(issue: CoderIssue) -> ValidatedIssue:
            # Build the seeded scaffold for THIS issue.
            issue_paths = set(issue.target_files) | set(issue.test_files)
            if use_repair_tier:
                # v4 fix #3 (2026-05-19): for REPAIR, read the LIVE
                # workspace state (not the planner's frozen seed).
                # This is the cross-fix-issue conflict source — agent
                # B used to see A's pre-fix content as its "seed" and
                # plan against stale assumptions. Reading from disk
                # gives B the post-A content.
                issue_seed = []
                for path in sorted(issue_paths):
                    content = _read_workspace_file(workspace, path)
                    if content is not None:
                        issue_seed.append(CtFilledFile(
                            path=path, content=content, role="code",
                        ))
                if not issue_seed:
                    # Fallback to planner seed if disk reads all failed.
                    issue_seed = [
                        CtFilledFile(
                            path=sf.path, content=sf.content, role="code",
                        )
                        for sf in seeded_files
                        if sf.path in issue_paths
                    ]
            else:
                # IMPLEMENT path unchanged: planner's frozen seed.
                issue_seed = [
                    CtFilledFile(
                        path=sf.path, content=sf.content, role="code",
                    )
                    for sf in seeded_files
                    if sf.path in issue_paths
                ]
            # Filter sibling list to exclude THIS issue.
            siblings = [s for s in sibling_summaries if not s.startswith(f"`{issue.id}` ")]
            # Build per-issue context snapshot (CTX-1, 2026-05-20).
            # Cheap (file IO + manifest parse); always fresh.
            try:
                ws_ctx = context_builder.build(issue)
                ctx_section = ws_ctx.to_prompt_section()
            except Exception as e:
                self._log(
                    f"V4: workspace_context build failed for [{issue.id}] "
                    f"({type(e).__name__}: {e}) — continuing without"
                )
                ctx_section = None

            # Initial agent dispatch. v4 fix B (2026-05-20): REPAIR
            # uses edit-mode (surgical patches against existing files).
            # IMPLEMENT stays whole-file (greenfield).
            initial = agent.code_issue(
                issue=issue,
                service=service,
                seeded_files=issue_seed,
                capabilities=list(enriched_spec.capabilities or []),
                skeleton_md=skeleton_md,
                auth_contract=auth_contract,
                sibling_issue_summaries=siblings,
                edit_mode=use_repair_tier,
                workspace_context_section=ctx_section,
            )
            # If edit-mode: apply ``edits`` (existing files) via
            # apply_edits AND write ``new_files`` (paths that don't
            # exist yet) wholesale. v5 hotfix 2026-05-20: prior
            # version silently swallowed file_missing failures and
            # reported false-clean; new_files closes that gap.
            if use_repair_tier and (initial.edits or initial.filled_files):
                from bizniz.coder_tester.edits import apply_edits
                from bizniz.coder_tester.types import (
                    CoderTesterResult, FilledFile,
                )
                # Step 1: write any new_files (carried in filled_files
                # for the edit-mode result envelope).
                new_files_to_write = list(initial.filled_files or [])
                for nf in new_files_to_write:
                    try:
                        workspace.write_file(nf.path, nf.content)
                    except Exception as e:
                        self._log(
                            f"V4 repair: [{issue.id}] failed to write "
                            f"new_file {nf.path}: {type(e).__name__}: {e}"
                        )
                # Step 2: apply edits against existing files. If
                # apply_edits reports file_missing failures, the agent
                # got the new_files split wrong — surface as a
                # CoderTesterError so the validator's fix-loop can
                # retry instead of reporting false-clean.
                if initial.edits:
                    report = apply_edits(workspace, initial.edits)
                    if report.failures:
                        # file_missing is the recoverable case — the
                        # agent should have put these in new_files.
                        # Other failure modes (no_match, ambiguous)
                        # mean the agent's old_text was wrong.
                        self._log(
                            f"V4 repair: [{issue.id}] {len(report.failures)} "
                            f"edit(s) failed: "
                            f"{[(f.path, f.reason) for f in report.failures[:3]]}"
                        )
                        # If ANY edits succeeded, partial progress is
                        # OK — let the validator scan and the loop
                        # decide. If NONE succeeded, raise so the
                        # fix-pass mechanism retries.
                        if not report.paths_written and not new_files_to_write:
                            from bizniz.coder_tester.agent import CoderTesterError
                            raise CoderTesterError(
                                f"CoderTesterAgent[{issue.id}, edit]: ALL "
                                f"edits failed to apply ({len(report.failures)} "
                                f"failure(s)). First: "
                                f"{report.failures[0].path} — "
                                f"{report.failures[0].reason}. "
                                f"Agent likely needs to use new_files "
                                f"instead of edits for missing paths."
                            )
                # Synthesize FilledFile[] from on-disk content for
                # the validator (which expects FilledFile).
                synth = []
                for path in (
                    list(issue.target_files) + list(issue.test_files)
                ):
                    content = _read_workspace_file(workspace, path)
                    if content is not None:
                        role = "test" if path in issue.test_files else "code"
                        synth.append(FilledFile(
                            path=path, content=content, role=role,
                        ))
                initial = CoderTesterResult(
                    issue_id=issue.id,
                    filled_files=synth,
                    notes=initial.notes,
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
