"""Per-service Orchestrator — drives the Coder issue-by-issue.

Responsibilities:
1. Topo-sort the service's issues by ``depends_on``.
2. For each issue: build a Coder bound to the current tier's model
   and dispatch ``code_issue()``.
3. On ``ToolLoopAgentStalledError``: escalate via ``ModelProgression``
   and retry with a stronger model. If the progression is exhausted,
   mark the issue stalled and move on.
4. If an issue's dependency stalled, mark dependents ``skipped``
   without dispatching the Coder (cheaper than letting them stall
   on missing imports).

The Orchestrator does NOT write code, run tests, or know about
specific tools. All of that lives in the Coder. This is purely a
loop + escalation policy.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Set

from bizniz.architect.types import SystemArchitecture
from bizniz.coder.agent import Coder
from bizniz.coder.types import CoderResult, Issue
from bizniz.lib.dependency_graph import topological_layers
from bizniz.lib.model_progression import ModelProgression
from bizniz.lib.tool_loop_agent import ToolLoopAgentStalledError
from bizniz.orchestrator.types import (
    IssueDisposition, IssueOutcome, OrchestratorResult,
)
from bizniz.quality_engineer.types import EnrichedSpec
from bizniz.state.issue_store import IssueStateStore, ResumeBehavior


CoderFactory = Callable[[str], Coder]
"""Function that builds a Coder bound to ``model_name``.

Construction lives outside the orchestrator because the Coder needs
a workspace + compose_path + service-specific configuration that
only the caller (MilestoneLoop) knows. The orchestrator just asks
for a Coder bound to a particular model.
"""


class Orchestrator:
    """Runs one service's worth of issues through the Coder."""

    def __init__(
        self,
        service: str,
        coder_factory: CoderFactory,
        progression: ModelProgression,
        issue_store: Optional[IssueStateStore] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._service = service
        self._coder_factory = coder_factory
        self._progression = progression
        self._issue_store = issue_store
        self._on_status = on_status

    # ── Public ─────────────────────────────────────────────────────────

    def run_service(
        self,
        issues: List[Issue],
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        auth_contract: Optional[str] = None,
        workspace_summary: Optional[str] = None,
        skeleton_md: Optional[str] = None,
    ) -> OrchestratorResult:
        """Dispatch every issue in topo order, return aggregate result."""
        result = OrchestratorResult(service=self._service)
        if not issues:
            return result

        layers = topological_layers(issues)
        failed_ids: Set[str] = set()

        for layer in layers:
            for issue in layer:
                if any(dep in failed_ids for dep in issue.depends_on):
                    self._log(
                        f"[{self._service}] {issue.id}: skipping — "
                        f"dependency previously failed."
                    )
                    result.issues.append(IssueOutcome(
                        issue_id=issue.id,
                        disposition="skipped",
                        error="upstream dependency stalled or failed",
                    ))
                    failed_ids.add(issue.id)
                    continue

                outcome = self._run_one_issue(
                    issue=issue,
                    architecture=architecture,
                    enriched_spec=enriched_spec,
                    auth_contract=auth_contract,
                    workspace_summary=workspace_summary,
                    skeleton_md=skeleton_md,
                )
                result.issues.append(outcome)
                # Only mark as failed for dependency purposes if the
                # issue actually FAILED. ``deferred`` is "intentionally
                # not run" (e.g. work was absorbed into a sibling fix
                # issue) and shouldn't block downstream issues. Real
                # failures: failed/partial/stalled/errored/skipped.
                # Treat ``deferred`` like ``passed``/``escalated`` for
                # dep satisfaction.
                if outcome.disposition not in ("passed", "escalated", "deferred"):
                    failed_ids.add(issue.id)
        return result

    # ── Per-issue ──────────────────────────────────────────────────────

    def _run_one_issue(
        self,
        issue: Issue,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        auth_contract: Optional[str],
        workspace_summary: Optional[str],
        skeleton_md: Optional[str],
    ) -> IssueOutcome:
        """Dispatch one issue, escalating on stall."""
        # Resume gate: if the DB shows this issue already terminal, return
        # the persisted outcome without invoking the Coder. Saves API spend
        # on resumed runs.
        if self._issue_store is not None:
            decision = self._issue_store.resume_decision(self._service, issue.id)
            if decision == ResumeBehavior.SKIP:
                prior = self._issue_store.previous_outcome(self._service, issue.id)
                if prior is not None:
                    self._log(
                        f"[{self._service}] {issue.id}: resume — already "
                        f"{prior.disposition} on previous run, skipping"
                    )
                    return prior

        # Always start each issue at the cheapest tier — passing on
        # one issue doesn't justify upgrading the whole service.
        self._progression.reset()
        tiers_used: List[str] = []
        last_err: str = ""

        while True:
            model = self._progression.current_model
            tiers_used.append(model)
            self._log(
                f"[{self._service}] {issue.id}: starting on tier "
                f"{self._progression._index} ({model})"
            )
            if self._issue_store is not None:
                self._issue_store.mark_started(self._service, issue.id, model)
            coder = self._coder_factory(model)

            try:
                result: CoderResult = coder.code_issue(
                    issue=issue,
                    architecture=architecture,
                    enriched_spec=enriched_spec,
                    auth_contract=auth_contract,
                    workspace_summary=workspace_summary,
                    skeleton_md=skeleton_md,
                )
            except ToolLoopAgentStalledError as e:
                last_err = str(e)
                self._log(
                    f"[{self._service}] {issue.id}: stalled on {model} — "
                    f"{last_err[:120]}"
                )
                if self._progression.is_at_max:
                    self._log(
                        f"[{self._service}] {issue.id}: progression exhausted, "
                        f"marking stalled."
                    )
                    if self._issue_store is not None:
                        self._issue_store.mark_finished(
                            self._service, issue.id,
                            status="stalled", error=last_err,
                        )
                    return IssueOutcome(
                        issue_id=issue.id,
                        disposition="stalled",
                        tiers_used=tiers_used,
                        error=last_err,
                    )
                next_model = self._progression.escalate()
                self._log(
                    f"[{self._service}] {issue.id}: escalating to {next_model}"
                )
                continue
            except Exception as e:
                err_str = f"{type(e).__name__}: {e}"
                # Transient class — provider overload (Gemini 503),
                # connection drops, timeouts. Escalate to the next
                # tier (typically a different model with separate
                # quota) instead of marking errored. Tier-0 lite is
                # the most overloaded; flash and flash-top usually
                # have headroom even when lite is 503'd.
                if self._is_transient_error(e) and not self._progression.is_at_max:
                    next_model = self._progression.escalate()
                    self._log(
                        f"[{self._service}] {issue.id}: transient error "
                        f"on {model} ({type(e).__name__}); escalating "
                        f"to {next_model}"
                    )
                    continue

                # Genuine error: log + record + give up on this issue.
                # Other issues continue.
                self._log(
                    f"[{self._service}] {issue.id}: errored — {err_str[:200]}"
                )
                if self._issue_store is not None:
                    self._issue_store.mark_finished(
                        self._service, issue.id,
                        status="errored", error=err_str,
                    )
                return IssueOutcome(
                    issue_id=issue.id,
                    disposition="errored",
                    tiers_used=tiers_used,
                    error=err_str,
                )

            # `passed` and `deferred` terminate. `partial` / `failed`
            # are escalation-eligible — the Coder either ran out of
            # iterations or self-reported short of the goal. Try the
            # next tier with fresh budget. Same exhaustion semantics
            # as the stall path.
            if result.status in ("partial", "failed"):
                last_err = result.summary or f"status={result.status}"
                self._log(
                    f"[{self._service}] {issue.id}: {result.status} on {model} — "
                    f"{last_err[:120]}"
                )
                if self._progression.is_at_max:
                    self._log(
                        f"[{self._service}] {issue.id}: progression exhausted, "
                        f"marking {result.status}."
                    )
                    if self._issue_store is not None:
                        self._issue_store.mark_finished(
                            self._service, issue.id,
                            status=result.status, result=result, error=last_err,
                        )
                    return IssueOutcome(
                        issue_id=issue.id,
                        disposition=result.status,
                        tiers_used=tiers_used,
                        final_result=result,
                        error=last_err,
                    )
                next_model = self._progression.escalate()
                self._log(
                    f"[{self._service}] {issue.id}: escalating to {next_model}"
                )
                continue

            disposition: IssueDisposition
            if result.status == "passed":
                disposition = "escalated" if len(tiers_used) > 1 else "passed"
            else:
                disposition = result.status  # deferred

            if self._issue_store is not None:
                self._issue_store.mark_finished(
                    self._service, issue.id,
                    status=disposition, result=result,
                )

            return IssueOutcome(
                issue_id=issue.id,
                disposition=disposition,
                tiers_used=tiers_used,
                final_result=result,
            )

    # ── Error classification ───────────────────────────────────────────

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        """True if ``exc`` is a provider-side blip we should retry by
        escalating tiers. Conservative — only matches signatures we've
        actually observed in the wild:
          - ToolLoopAgentBadResponseError("LLM call failed N times")
            raised after the Coder hit 3 consecutive Gemini 503s.
          - 503 / UNAVAILABLE / overload language in the message.
          - Connection-class network errors (reset, timeout).
        """
        msg = str(exc).lower()
        cls = type(exc).__name__
        if "llm call failed" in msg:
            return True
        if "503" in msg or "unavailable" in msg or "overload" in msg:
            return True
        if "connection" in msg or "timeout" in msg or "reset" in msg:
            return True
        if cls in ("ConnectionError", "TimeoutError", "OSError"):
            return True
        return False

    # ── Status ─────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass
