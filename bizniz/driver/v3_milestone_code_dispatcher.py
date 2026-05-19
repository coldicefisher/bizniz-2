"""``V3MilestoneCodeDispatcher`` — v3 pipeline IMPLEMENT phase.

Drop-in replacement for ``MilestoneCodeDispatcher`` that uses the
v3 single-dispatch agents instead of per-issue Coder loops:

  ServicePlannerWithScaffold (per service)
       ↓ emits issues + seeded scaffold
  [seed materialized to workspace]
       ↓
  CoderAgentV3 (per service, single call, fills all bodies)
       ↓
  All issues marked passed/errored in IssueStateStore
       ↓
  EngineerResult returned to MilestoneLoop

Stage A of the v3 ship (2026-05-19). Replaces ONLY the IMPLEMENT
phase. Downstream phases (review_repair, integration, UX, refactor)
read from the same IssueStateStore + workspace, so they don't
notice the upstream swap. Old MilestoneCodeDispatcher remains the
default; v3 lives behind ``--use-v3-implement`` flag in v2_build.

Anchor data (recipe_v3 M1 backend + recipe_v2 M3 backend):
  v2 IMPLEMENT (24 sequential Coder dispatches): 1h 35m
  v3 IMPLEMENT (1 ServicePlanner + 1 CoderAgent per service):
    - M1 (Phase 2a): 1m 55s for 7 issues, 8 files
    - M3 (Phase 2c): 7m 42s for 9 issues, 5 files, full chain

Same shape as v2's dispatcher so MilestoneLoop swaps with one
constructor arg.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.coder.agent_v3 import CoderAgentV3, FilledFile
from bizniz.coder.types import CoderResult, Issue as CoderIssue
from bizniz.engineer.types import (
    EngineerPlan, EngineerResult, Issue as EngineerIssue,
)
from bizniz.lib.dependency_graph import topological_layers
from bizniz.quality_engineer.types import EnrichedSpec
from bizniz.service_planner.scaffolded import ServicePlannerWithScaffold
from bizniz.state.issue_store import IssueStateStore
from bizniz.workspace.base_workspace import BaseWorkspace


# Factory shape mirrors v2's: each returns an instance the dispatcher
# will use. Separate factories so v2_build can wire the right client
# per agent + per service (e.g., Opus for the planner, Haiku→Opus
# escalation for the coder).
ServicePlannerV3Factory = Callable[[ServiceDefinition], ServicePlannerWithScaffold]
CoderAgentV3Factory = Callable[[ServiceDefinition], CoderAgentV3]
WorkspaceForService = Callable[[str], BaseWorkspace]


def _is_code_bearing(service: ServiceDefinition) -> bool:
    """Same gate as v2 — skip yaml/sql/infrastructure services."""
    lang = (service.language or "").lower()
    return lang not in {"yaml", "sql"}


class V3MilestoneCodeDispatcher:
    """v3 IMPLEMENT phase: one ServicePlanner + one CoderAgent per
    code-bearing service. Writes the same IssueStateStore rows the v2
    dispatcher wrote so downstream phases (review_repair etc.) read
    a uniform shape."""

    def __init__(
        self,
        *,
        planner_factory: ServicePlannerV3Factory,
        coder_factory: CoderAgentV3Factory,
        workspace_for_service: WorkspaceForService,
        issue_store: Optional[IssueStateStore] = None,
        on_status: Optional[Callable[[str], None]] = None,
        only_service: Optional[str] = None,
        repair_dispatcher=None,
    ):
        self._planner_factory = planner_factory
        self._coder_factory = coder_factory
        self._workspace_for_service = workspace_for_service
        self._issue_store = issue_store
        self._on_status = on_status
        self._only_service = only_service
        # Stage A scope: v3 handles IMPLEMENT only. The review/repair
        # phase still uses today's per-issue dispatch. ``MilestoneLoop``
        # calls ``code_dispatcher.repair(...)`` during review_repair,
        # so we delegate that to an internal v2 dispatcher. Stage B
        # replaces this with the parallel review unit + batch-fix
        # debugger entirely.
        self._repair_dispatcher = repair_dispatcher

    def repair(self, **kwargs):
        """Delegate to the v2 dispatcher injected at construction.

        Stage A keeps today's review/repair loop intact. Without this
        delegate, MilestoneLoop's review_repair phase would crash on
        AttributeError after IMPLEMENT succeeds. The v2 dispatcher
        does the right thing — replan with ServicePlanner.repair,
        dispatch per-issue Coders for the fix-issues.
        """
        if self._repair_dispatcher is None:
            raise RuntimeError(
                "V3MilestoneCodeDispatcher.repair called but no v2 "
                "repair_dispatcher was injected. Pass one in the "
                "constructor or wire Stage B's review unit before "
                "removing the v2 fallback."
            )
        return self._repair_dispatcher.repair(**kwargs)

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

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
        """Same input shape as v2 ``MilestoneCodeDispatcher.run``.

        ``workspace_summary`` is accepted for compat but unused in v3
        (the seeded scaffold replaces "what's already in the workspace"
        context).
        """
        self._log("V3MilestoneCodeDispatcher: starting")
        active_store = issue_store if issue_store is not None else self._issue_store

        layers = topological_layers(list(architecture.services))
        all_issues: List[EngineerIssue] = []
        completed_units: List[str] = []
        deferred_units: List[str] = []
        unit_to_parent: Dict[str, str] = {}
        per_service_summaries: List[str] = []
        per_service_notes: List[str] = []

        for layer in layers:
            for service in layer:
                if not _is_code_bearing(service):
                    self._log(
                        f"V3MilestoneCodeDispatcher: skipping `{service.name}` "
                        f"(language='{service.language}' is infrastructure-only)"
                    )
                    continue
                if (self._only_service is not None
                        and service.name != self._only_service):
                    self._log(
                        f"V3MilestoneCodeDispatcher: skipping `{service.name}` "
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
                )
                completed_units.extend(result["completed"])
                deferred_units.extend(result["deferred"])
                for iid in result["completed"]:
                    unit_to_parent[iid] = iid
                for iid in result["deferred"]:
                    unit_to_parent[iid] = iid
                # Build EngineerIssue records from the planned issues.
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

        # Synthesize an EngineerPlan + EngineerResult. ServicePlanner
        # doesn't emit a narrative approach separately, so we summarize.
        plan = EngineerPlan(
            approach=(
                f"v3 IMPLEMENT (single-dispatch CoderAgent) on "
                f"{len(all_issues)} issues across "
                f"{len(per_service_summaries)} service(s). Each service "
                f"gets one ServicePlannerWithScaffold call producing "
                f"issues + seeded scaffold, then one CoderAgentV3 call "
                f"filling all bodies."
            ),
            issues=all_issues,
        )

        if deferred_units:
            final_status = "partial"
        elif completed_units:
            final_status = "passed"
        else:
            final_status = "not_run"

        return EngineerResult(
            plan=plan,
            summary="\n".join(per_service_summaries),
            final_test_status=final_status,
            completed_issue_ids=list(completed_units),
            deferred_issue_ids=list(deferred_units),
            completed_units=list(completed_units),
            deferred_units=list(deferred_units),
            notes=per_service_notes,
        )

    # ── Internals ─────────────────────────────────────────────────

    def _dispatch_service(
        self,
        *,
        service: ServiceDefinition,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        skeleton_md: Optional[str],
        auth_contract: Optional[str],
        active_store: Optional[IssueStateStore],
    ) -> dict:
        """Run the two-stage v3 dispatch for one service.

        Returns {"issues", "completed", "deferred", "wall_s", "notes"}.
        """
        t0 = time.time()
        planner = self._planner_factory(service)
        self._log(
            f"V3MilestoneCodeDispatcher: planning + seeding `{service.name}`"
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
                f"V3MilestoneCodeDispatcher: `{service.name}` PLANNER failed "
                f"in {wall:.1f}s: {type(e).__name__}: {e}"
            )
            return {
                "issues": [],
                "completed": [],
                "deferred": [],
                "wall_s": wall,
                "notes": [f"planner failed: {type(e).__name__}: {e}"],
            }

        issues = plan_result.issues
        seeded_files = plan_result.seeded_files
        self._log(
            f"V3MilestoneCodeDispatcher: `{service.name}` planned "
            f"{len(issues)} issue(s), {len(seeded_files)} seeded file(s)"
        )

        # Persist planned issues to the store so downstream phases see them.
        if active_store is not None:
            try:
                active_store.record_planned(service.name, issues)
            except Exception as e:
                self._log(
                    f"V3MilestoneCodeDispatcher: record_planned failed "
                    f"(non-fatal): {type(e).__name__}: {e}"
                )

        # Materialize seeded scaffold onto the workspace BEFORE CoderAgent
        # runs. CoderAgent reads the same seed content from prompt
        # context, but having files on disk lets debugger / downstream
        # phases see the contract too.
        try:
            workspace = self._workspace_for_service(service.workspace_name)
            ws_root = getattr(workspace, "root", None)
            if ws_root is not None:
                from pathlib import Path
                ws_path = Path(str(ws_root))
                ws_path.mkdir(parents=True, exist_ok=True)
                for sf in seeded_files:
                    dest = ws_path / sf.path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(sf.content, encoding="utf-8")
        except Exception as e:
            self._log(
                f"V3MilestoneCodeDispatcher: seed materialization warning "
                f"(continuing): {type(e).__name__}: {e}"
            )

        # Mark each issue started — the v3 dispatch is one logical
        # call, but per-issue tracking lets downstream phases attribute.
        if active_store is not None:
            for issue in issues:
                try:
                    active_store.mark_started(
                        service.name, issue.id, "claude-cli:v3",
                    )
                except Exception:
                    pass

        # Dispatch CoderAgentV3. Convert the planner's SeededFile
        # records to CoderAgentV3's FilledFile (same shape, different
        # Pydantic class) so the coder's input typing checks pass.
        coder_seeded = [
            FilledFile(path=sf.path, content=sf.content)
            for sf in seeded_files
        ]
        coder = self._coder_factory(service)
        try:
            fill_result = coder.fill_milestone(
                architecture=architecture,
                enriched_spec=enriched_spec,
                service=service,
                issues=issues,
                seeded_files=coder_seeded,
                skeleton_md=skeleton_md,
                auth_contract=auth_contract,
            )
        except Exception as e:
            wall = time.time() - t0
            self._log(
                f"V3MilestoneCodeDispatcher: `{service.name}` CODER failed "
                f"in {wall:.1f}s: {type(e).__name__}: {e}"
            )
            deferred = [issue.id for issue in issues]
            if active_store is not None:
                for issue_id in deferred:
                    try:
                        active_store.mark_finished(
                            service.name, issue_id,
                            status="errored",
                            error=f"{type(e).__name__}: {e}",
                        )
                    except Exception:
                        pass
            return {
                "issues": issues,
                "completed": [],
                "deferred": deferred,
                "wall_s": wall,
                "notes": [f"coder failed: {type(e).__name__}: {e}"],
            }

        # Materialize CoderAgent's filled files to the workspace,
        # overwriting the stub bodies.
        try:
            from pathlib import Path
            workspace = self._workspace_for_service(service.workspace_name)
            ws_root = getattr(workspace, "root", None)
            if ws_root is not None:
                ws_path = Path(str(ws_root))
                for ff in fill_result.filled_files:
                    dest = ws_path / ff.path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(ff.content, encoding="utf-8")
        except Exception as e:
            self._log(
                f"V3MilestoneCodeDispatcher: fill materialization "
                f"failed: {type(e).__name__}: {e}"
            )

        # An issue is "completed" when ALL of its target_files appear
        # in the CoderAgent's filled_files output. Partial = deferred.
        filled_paths = {ff.path for ff in fill_result.filled_files}
        completed: List[str] = []
        deferred: List[str] = []
        for issue in issues:
            if all(tf in filled_paths for tf in issue.target_files):
                completed.append(issue.id)
            else:
                deferred.append(issue.id)

        # Mark finished status per issue.
        if active_store is not None:
            for issue in issues:
                status = "passed" if issue.id in completed else "errored"
                # Synthesize a CoderResult for the store (it expects one).
                synth = CoderResult(
                    issue_id=issue.id,
                    status="passed" if status == "passed" else "errored",
                    target_files_written=[
                        tf for tf in issue.target_files
                        if tf in filled_paths
                    ],
                    test_files_written=[
                        tf for tf in issue.test_files
                        if tf in filled_paths
                    ],
                    summary=(
                        f"v3 single-dispatch: filled by CoderAgentV3 "
                        f"(milestone-level call, {len(fill_result.filled_files)} "
                        f"file(s) total)"
                    ),
                    notes=[],
                    tier_used=0,
                    iterations_used=0,
                    unresolved_symbols_at_exit=[],
                    last_test_output_tail="",
                )
                try:
                    active_store.mark_finished(
                        service.name, issue.id,
                        status=status,
                        result=synth,
                        error="" if status == "passed" else "target_files not present in CoderAgent output",
                    )
                except Exception:
                    pass

        wall = time.time() - t0
        self._log(
            f"V3MilestoneCodeDispatcher: `{service.name}` filled "
            f"{len(completed)}/{len(issues)} issue(s) in {wall:.1f}s "
            f"(deferred {len(deferred)})"
        )
        return {
            "issues": issues,
            "completed": completed,
            "deferred": deferred,
            "wall_s": wall,
            "notes": [],
        }
