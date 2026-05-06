"""Engineer — milestone-scoped tool-loop agent.

ONE Engineer per milestone. Gets the full EnrichedSpec, architecture,
auth contract, and prior contracts. Plans first (mandatory), then
iterates with full discovery + mutation + test + container tools.
Emits a typed ``EngineerResult`` when done.

The plan-first invariant is enforced in ``_dispatch_action`` rather
than via the schema (the schema can't express ordering). Until
``submit_plan`` arrives, all other actions are rejected with a
correction message; the LLM is told to submit a plan.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Iterable, List, Literal, Optional

from bizniz.architect.types import SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.lib.tool_loop_agent import ToolHandler, ToolLoopAgent
from bizniz.lib.tools.container import build_container_handlers
from bizniz.lib.tools.database import build_database_handlers
from bizniz.lib.tools.discovery import build_discovery_handlers
from bizniz.lib.tools.file_io import build_file_io_handlers
from bizniz.lib.tools.jwt import build_jwt_handlers
from bizniz.lib.tools.test_runner import build_test_handlers
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.types import EnrichedSpec
from bizniz.workspace.base_workspace import BaseWorkspace

from bizniz.engineer.prompts.initial_context import (
    build_engineer_initial_context,
    build_engineer_repair_context,
)
from bizniz.engineer.prompts.schema import ENGINEER_ACTION_SCHEMA
from bizniz.engineer.prompts.system_prompt import (
    ENGINEER_REPAIR_SYSTEM_PROMPT,
    ENGINEER_SYSTEM_PROMPT,
)
from bizniz.engineer.types import (
    EngineerError,
    EngineerPlan,
    EngineerResult,
    Issue,
)


EngineerMode = Literal["implement", "repair"]


class Engineer(ToolLoopAgent):
    """Milestone-scoped tool-loop agent.

    Construct once with the workspace + LLM client + auth/test infra.
    Call ``implement(milestone, ...)`` to run the agent against one
    milestone. Returns ``EngineerResult``.

    Plan-first invariant: the agent MUST submit ``submit_plan`` as its
    first non-discovery action. Until then, the loop wraps every other
    action with a correction message asking for a plan.
    """

    def __init__(
        self,
        client: BaseAIClient,
        workspace: BaseWorkspace,
        compose_path: str,
        target_service: str,
        on_status: Optional[Callable[[str], None]] = None,
        tool_iterations: int = 60,
        timeout_seconds: int = 1800,
        base_url: Optional[str] = None,
        history_window: int = 12,
    ):
        """``history_window`` (default 12) bounds per-call input growth
        via sliding-window compaction (lever B from cost analysis). The
        Engineer keeps the system prompt + initial user message + the
        last 12 assistant/user pairs. Older tool results get dropped
        with a synthetic "compacted, use discovery tools to re-fetch"
        note. Set to 0 to disable (legacy unbounded growth)."""
        super().__init__(
            client=client,
            workspace=workspace,
            on_status=on_status,
            tool_iterations=tool_iterations,
            timeout_seconds=timeout_seconds,
            history_window=history_window,
        )
        self._compose_path = compose_path
        self._target_service = target_service
        self._base_url = base_url
        # Per-call state. Set in implement() / repair().
        self._mode: EngineerMode = "implement"
        self._plan: Optional[EngineerPlan] = None
        self._handlers: Dict[str, ToolHandler] = {}

    # ── ToolLoopAgent contract ────────────────────────────────────────

    @property
    def system_prompt(self) -> str:
        if self._mode == "repair":
            return ENGINEER_REPAIR_SYSTEM_PROMPT
        return ENGINEER_SYSTEM_PROMPT

    @property
    def action_schema(self) -> dict:
        return ENGINEER_ACTION_SCHEMA

    @property
    def terminal_action(self) -> str:
        return "submit_implementation"

    def tool_handlers(self) -> Dict[str, ToolHandler]:
        return self._handlers

    def parse_terminal_action(self, action: dict) -> EngineerResult:
        if self._plan is None:
            # Should never happen: plan-first guard prevents this. But
            # if it does, fail loudly rather than ship an unplanned
            # result.
            raise EngineerError(
                "submit_implementation reached without a submitted plan."
            )
        return EngineerResult(
            plan=self._plan,
            summary=action.get("summary") or "",
            final_test_status=action.get("final_test_status") or "not_run",
            completed_issue_ids=list(action.get("completed_issue_ids") or []),
            deferred_issue_ids=list(action.get("deferred_issue_ids") or []),
            notes=list(action.get("notes") or []),
        )

    # ── Public entry ──────────────────────────────────────────────────

    def implement(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        auth_contract: Optional[str] = None,
        prior_specs: Optional[Iterable[EnrichedSpec]] = None,
        workspace_summary: Optional[str] = None,
    ) -> EngineerResult:
        """Run the Engineer against one milestone (implement mode).

        Returns the typed ``EngineerResult``. Caller is responsible for
        downstream review (QualityEngineer.review + CodeReviewer.review)
        and persistence.
        """
        self._log(f"Engineer: starting milestone '{milestone.name}' (implement)")
        self._mode = "implement"
        self._plan = None
        self._handlers = self._build_handlers()

        initial = build_engineer_initial_context(
            milestone=milestone,
            architecture=architecture,
            enriched_spec=enriched_spec,
            auth_contract=auth_contract,
            prior_specs=prior_specs,
            workspace_summary=workspace_summary,
        )

        result: EngineerResult = self.run(initial)
        self._log(
            f"Engineer: done — {len(result.completed_issue_ids)} completed, "
            f"{len(result.deferred_issue_ids)} deferred, "
            f"final_test_status={result.final_test_status}"
        )
        return result

    def repair(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        code_review_report: CodeReviewReport,
        enriched_spec: Optional[EnrichedSpec] = None,
        auth_contract: Optional[str] = None,
        prior_specs: Optional[Iterable[EnrichedSpec]] = None,
    ) -> EngineerResult:
        """Run the Engineer in repair mode against a CodeReviewReport.

        The Engineer reads the report, plans targeted fixes (one issue
        per finding or cluster), implements them, and submits. Plan-first
        invariant still applies; ``spec_refs`` are optional in repair
        mode (issues describe fixes, not new behavior).

        ``enriched_spec`` is optional but recommended — gives the
        repair Engineer context about what the milestone is meant to
        do, so it can fix things consistent with the original intent.
        """
        self._log(
            f"Engineer: starting milestone '{milestone.name}' (repair, "
            f"{code_review_report.total_findings} findings, "
            f"{len(code_review_report.critical_findings)} critical)"
        )
        self._mode = "repair"
        self._plan = None
        self._handlers = self._build_handlers()

        initial = build_engineer_repair_context(
            milestone=milestone,
            architecture=architecture,
            code_review_report=code_review_report,
            enriched_spec=enriched_spec,
            auth_contract=auth_contract,
            prior_specs=prior_specs,
        )

        result: EngineerResult = self.run(initial)
        self._log(
            f"Engineer (repair): done — {len(result.completed_issue_ids)} fixed, "
            f"{len(result.deferred_issue_ids)} deferred, "
            f"final_test_status={result.final_test_status}"
        )
        return result

    # ── Tool surface ──────────────────────────────────────────────────

    def _build_handlers(self) -> Dict[str, ToolHandler]:
        """Wire the full Engineer tool surface.

        Composes existing factories + adds plan/get_my_plan/revise_plan
        actions backed by closures over ``self``. A plan-first guard
        wraps every non-plan action so attempts before submit_plan fall
        through to a correction message.
        """
        h: Dict[str, ToolHandler] = {}
        h.update(build_file_io_handlers(self._workspace))
        h.update(build_discovery_handlers(self._workspace))
        h.update(build_container_handlers(self._compose_path, self._target_service))
        h.update(
            build_test_handlers(
                compose_path=self._compose_path,
                workspace_path=Path(self._workspace.root)
                if hasattr(self._workspace, "root") else Path("."),
                target_service=self._target_service,
                base_url=self._base_url,
            )
        )
        h.update(build_database_handlers(self._compose_path))
        h.update(build_jwt_handlers())
        h["submit_plan"] = self._handle_submit_plan
        h["revise_plan"] = self._handle_revise_plan
        h["get_my_plan"] = self._handle_get_my_plan
        return self._guard_plan_first(h)

    def _guard_plan_first(
        self, handlers: Dict[str, ToolHandler],
    ) -> Dict[str, ToolHandler]:
        """Wrap mutation + test handlers so they refuse to run before
        ``submit_plan``. Discovery is allowed pre-plan so the Engineer
        can read context to inform the plan.
        """
        gated = {
            "write_file",
            "run_tests",
            "smoke_import",
            "run_in_container",
            "run_python_in_container",
            "hit_endpoint",
            "inspect_env",
            "tail_logs",
            "query_database",
            "decode_jwt",
            "revise_plan",
            "get_my_plan",
        }
        out: Dict[str, ToolHandler] = {}
        for name, handler in handlers.items():
            if name in gated:
                out[name] = self._make_guarded(name, handler)
            else:
                out[name] = handler
        return out

    def _make_guarded(self, name: str, inner: ToolHandler) -> ToolHandler:
        def guarded(action: dict) -> str:
            if self._plan is None:
                return (
                    f"REJECTED: '{name}' is not allowed before you have "
                    f"submitted a plan. Your next action MUST be "
                    f"'submit_plan'."
                )
            return inner(action)
        return guarded

    # ── Plan handlers ────────────────────────────────────────────────

    def _handle_submit_plan(self, action: dict) -> str:
        if self._plan is not None:
            return (
                "REJECTED: a plan has already been submitted. Use "
                "'revise_plan' to update it."
            )
        try:
            plan = self._parse_plan(action)
        except EngineerError as e:
            return f"REJECTED: {e}. Please re-emit submit_plan."
        self._plan = plan
        self._log(
            f"Engineer: plan submitted — {len(plan.issues)} issue(s): "
            + ", ".join(i.id for i in plan.issues)
        )
        return self._render_plan(plan, header="PLAN ACCEPTED")

    def _handle_revise_plan(self, action: dict) -> str:
        try:
            plan = self._parse_plan(action)
        except EngineerError as e:
            return f"REJECTED: {e}. Please re-emit revise_plan."
        prior_ids = {i.id for i in (self._plan.issues if self._plan else [])}
        new_ids = {i.id for i in plan.issues}
        added = sorted(new_ids - prior_ids)
        removed = sorted(prior_ids - new_ids)
        self._plan = plan
        self._log(
            f"Engineer: plan revised — added={added or '—'}, "
            f"removed={removed or '—'}"
        )
        return self._render_plan(
            plan,
            header=f"PLAN REVISED (added={added}, removed={removed})",
        )

    def _handle_get_my_plan(self, action: dict) -> str:
        if self._plan is None:
            return "(no plan submitted yet)"
        return self._render_plan(self._plan, header="CURRENT PLAN")

    # ── Parsing / rendering ──────────────────────────────────────────

    def _parse_plan(self, action: dict) -> EngineerPlan:
        approach = (action.get("approach") or "").strip()
        if not approach:
            raise EngineerError("'approach' is required and must be non-empty")
        raw_issues = action.get("issues") or []
        if not raw_issues:
            raise EngineerError("'issues' must contain at least one issue")
        issues: List[Issue] = []
        seen_ids: set = set()
        for raw in raw_issues:
            try:
                issue = Issue.model_validate(raw)
            except Exception as e:
                raise EngineerError(f"issue failed validation: {e}") from e
            if issue.id in seen_ids:
                raise EngineerError(f"duplicate issue id: {issue.id}")
            seen_ids.add(issue.id)
            # spec_refs required in implement mode (every issue must
            # deliver a spec capability). Optional in repair mode (the
            # issue describes a fix, not new behavior).
            if not issue.spec_refs and self._mode != "repair":
                raise EngineerError(
                    f"issue '{issue.id}' has no spec_refs — every issue "
                    f"must reference at least one EnrichedSpec capability"
                )
            issues.append(issue)
        # Sanity: depends_on ids must reference issues in this plan.
        for issue in issues:
            for dep in issue.depends_on:
                if dep not in seen_ids:
                    raise EngineerError(
                        f"issue '{issue.id}' depends on unknown issue '{dep}'"
                    )
        return EngineerPlan(approach=approach, issues=issues)

    def _render_plan(self, plan: EngineerPlan, header: str) -> str:
        lines = [f"=== {header} ===", "", f"Approach: {plan.approach}", "", "Issues:"]
        for i in plan.issues:
            status_marker = {
                "pending": "[ ]",
                "in_progress": "[~]",
                "done": "[x]",
                "blocked": "[!]",
                "skipped": "[-]",
            }.get(i.status, "[?]")
            spec = ", ".join(i.spec_refs) or "—"
            deps = ", ".join(i.depends_on) or "—"
            lines.append(f"  {status_marker} {i.id}: {i.title}")
            lines.append(f"      spec_refs: {spec}")
            lines.append(f"      depends_on: {deps}")
            if i.target_files:
                lines.append(f"      target_files: {', '.join(i.target_files)}")
            if i.test_files:
                lines.append(f"      test_files: {', '.join(i.test_files)}")
        return "\n".join(lines)
