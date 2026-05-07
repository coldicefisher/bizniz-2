"""Issue-state persistence for the v2.5 IMPLEMENT phase.

Wraps ProjectDB's ``coder_issues`` table with a typed API the
dispatcher and milestone loop call. The DB is the SINGLE source of
truth for IMPLEMENT-phase state; the JSON state files no longer
carry an EngineerResult payload for IMPLEMENT.

Resume semantics:
  - ``passed`` / ``escalated`` / ``deferred`` → terminal, skip on resume
  - ``partial`` / ``failed`` / ``stalled`` / ``errored`` → retry, but
    pick up where escalation left off (last tier, not back to lite)
  - ``running`` → was killed mid-attempt; retry from current tier
  - ``pending`` → never started, normal dispatch
  - ``skipped`` → upstream failed; retry only if upstream now passes

The store is intentionally narrow: dispatcher and orchestrator call
``record_planned`` once per service, then ``mark_started`` and
``mark_finished`` per attempt. ``assemble_engineer_result`` rebuilds
the v2-shaped payload from DB rows so downstream review/repair
phases keep working unchanged.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import TYPE_CHECKING, List, Optional

from bizniz.coder.types import CoderResult, Issue
from bizniz.engineer.types import (
    EngineerPlan, EngineerResult, Issue as EngineerIssue,
)

if TYPE_CHECKING:
    from bizniz.orchestrator.types import IssueOutcome


# Terminal statuses — issues with these dispositions have a recorded
# final outcome. Resume skips them, MilestoneLoop considers IMPLEMENT
# done. The only non-terminal states are ``pending`` (never started)
# and ``running`` (was killed mid-attempt; resume should redo from
# scratch since the workspace may be in a partial state).
_TERMINAL_STATUSES = {
    "passed", "escalated", "deferred",
    "partial", "failed", "stalled", "errored", "skipped",
}


class ResumeBehavior(str, Enum):
    """How to treat an existing row when the dispatcher revisits it."""
    SKIP = "skip"           # row is terminal, do not redispatch
    REDISPATCH = "redispatch"  # row is non-terminal, run again


class IssueStateStore:
    """Typed wrapper around ProjectDB.coder_issues.

    Holds a ``job_id`` + ``milestone_index`` so callers don't have to
    pass them on every call. Construct one per (job, milestone) pair.
    """

    def __init__(self, *, db, job_id: str, milestone_index: int):
        self._db = db
        self._job_id = job_id
        self._milestone_index = milestone_index

    # ── Planning ──────────────────────────────────────────────────────

    def record_planned(self, service: str, issues: List[Issue]) -> None:
        """Persist the ServicePlanner's output for ``service``.

        Idempotent — re-planning updates the issue definitions but
        leaves runtime state (status, tiers_used, files_written, etc.)
        untouched. So a resume that re-plans first won't lose progress.
        """
        for idx, issue in enumerate(issues):
            self._db.upsert_planned_issue(
                job_id=self._job_id,
                milestone_index=self._milestone_index,
                service=service,
                issue_id=issue.id,
                issue_index=idx,
                title=issue.title,
                description=issue.description,
                language=issue.language or "python",
                target_files=list(issue.target_files),
                test_files=list(issue.test_files),
                spec_refs=list(issue.spec_refs),
                depends_on=list(issue.depends_on),
                success_criteria=list(issue.success_criteria),
            )

    # ── Resume gating ─────────────────────────────────────────────────

    def resume_decision(self, service: str, issue_id: str) -> ResumeBehavior:
        row = self._db.get_coder_issue(
            self._job_id, self._milestone_index, service, issue_id,
        )
        if row is None:
            return ResumeBehavior.REDISPATCH
        if row["status"] in _TERMINAL_STATUSES:
            return ResumeBehavior.SKIP
        return ResumeBehavior.REDISPATCH

    def previous_outcome(self, service: str, issue_id: str) -> Optional["IssueOutcome"]:
        """If we already have a terminal row for this issue, return its
        IssueOutcome so the orchestrator can record it without re-running.
        """
        row = self._db.get_coder_issue(
            self._job_id, self._milestone_index, service, issue_id,
        )
        if row is None or row["status"] not in _TERMINAL_STATUSES:
            return None
        return _row_to_outcome(row)

    # ── Per-attempt state transitions ─────────────────────────────────

    def mark_started(self, service: str, issue_id: str, tier: str) -> None:
        self._db.mark_issue_started(
            job_id=self._job_id,
            milestone_index=self._milestone_index,
            service=service, issue_id=issue_id, tier=tier,
        )

    def mark_finished(
        self,
        service: str,
        issue_id: str,
        *,
        status: str,
        result: Optional[CoderResult] = None,
        error: str = "",
    ) -> None:
        target_files_written: Optional[List[str]] = None
        test_files_written: Optional[List[str]] = None
        last_test_output = ""
        summary = ""
        notes: Optional[List[str]] = None
        if result is not None:
            target_files_written = list(result.target_files_written)
            test_files_written = list(result.test_files_written)
            last_test_output = result.last_test_output_tail
            summary = result.summary
            notes = list(result.notes)
        self._db.mark_issue_finished(
            job_id=self._job_id,
            milestone_index=self._milestone_index,
            service=service, issue_id=issue_id,
            status=status,
            target_files_written=target_files_written,
            test_files_written=test_files_written,
            last_test_output=last_test_output,
            summary=summary,
            error=error,
            notes=notes,
        )

    # ── Reads ─────────────────────────────────────────────────────────

    def all_rows(self, service: Optional[str] = None) -> list:
        return self._db.list_coder_issues(
            self._job_id, self._milestone_index, service=service,
        )

    def is_implement_done(self) -> bool:
        """True iff every planned issue has a terminal status. False if
        any are pending/running/non-terminal — i.e. dispatcher should
        run again."""
        rows = self.all_rows()
        if not rows:
            return False
        return all(r["status"] in _TERMINAL_STATUSES for r in rows)

    def assemble_engineer_result(self) -> EngineerResult:
        """Rebuild a v2-shaped EngineerResult from DB rows so the rest
        of MilestoneLoop (review, repair, integration) keeps working.

        Status mapping mirrors MilestoneCodeDispatcher's translation:
          passed/escalated → "done", partial → "in_progress",
          stalled/errored/failed → "blocked", skipped → "skipped",
          deferred → "skipped".
        """
        rows = self.all_rows()
        issues: list[EngineerIssue] = []
        completed: list[str] = []
        deferred_ids: list[str] = []

        for r in rows:
            disp = r["status"]
            if disp in ("passed", "escalated"):
                eng_status = "done"
                completed.append(r["issue_id"])
            elif disp == "partial":
                eng_status = "in_progress"
                deferred_ids.append(r["issue_id"])
            elif disp in ("stalled", "errored", "failed"):
                eng_status = "blocked"
                deferred_ids.append(r["issue_id"])
            elif disp == "skipped":
                eng_status = "skipped"
                deferred_ids.append(r["issue_id"])
            elif disp == "deferred":
                eng_status = "skipped"
                deferred_ids.append(r["issue_id"])
            else:
                eng_status = "pending"
                deferred_ids.append(r["issue_id"])

            issues.append(EngineerIssue(
                id=r["issue_id"],
                title=r["title"],
                description=r["description"],
                target_files=json.loads(r["target_files"] or "[]"),
                test_files=json.loads(r["test_files"] or "[]"),
                success_criteria=json.loads(r["success_criteria"] or "[]"),
                spec_refs=json.loads(r["spec_refs"] or "[]"),
                depends_on=json.loads(r["depends_on"] or "[]"),
                status=eng_status,
            ))

        total = len(rows)
        if total == 0:
            final_status = "not_run"
        elif len(completed) == total:
            final_status = "passed"
        elif len(completed) == 0:
            final_status = "failed"
        else:
            final_status = "partial"

        approach = "; ".join(
            f"{svc}: {len([r for r in rows if r['service'] == svc and r['status'] in _TERMINAL_STATUSES and r['status'] != 'deferred'])}/{len([r for r in rows if r['service'] == svc])}"
            for svc in sorted({r["service"] for r in rows})
        ) or "no services"
        notes = [
            f"[{r['service']}] {r['issue_id']} {r['status']}; "
            f"tiers: {' → '.join(json.loads(r['tiers_used'] or '[]')) or 'none'}"
            f"{(' — ' + r['error'][:120]) if r['error'] else ''}"
            for r in rows if r["status"] not in _TERMINAL_STATUSES
            or r["status"] == "deferred"
        ]
        summary = " · ".join(
            f"{svc}: {sum(1 for r in rows if r['service']==svc and r['status'] in ('passed','escalated'))}/"
            f"{sum(1 for r in rows if r['service']==svc)}"
            for svc in sorted({r["service"] for r in rows})
        )

        return EngineerResult(
            plan=EngineerPlan(approach=approach, issues=issues),
            summary=summary,
            final_test_status=final_status,
            completed_issue_ids=completed,
            deferred_issue_ids=deferred_ids,
            notes=notes,
        )


def _row_to_outcome(row) -> "IssueOutcome":
    """Build an IssueOutcome from a coder_issues row."""
    from bizniz.orchestrator.types import IssueOutcome  # deferred to break cycle
    tiers = json.loads(row["tiers_used"] or "[]")
    final_result: Optional[CoderResult] = None
    if row["status"] in ("passed", "escalated", "partial", "failed", "deferred"):
        final_result = CoderResult(
            issue_id=row["issue_id"],
            status=("passed" if row["status"] == "escalated"
                    else row["status"]),
            target_files_written=json.loads(row["target_files_written"] or "[]"),
            test_files_written=json.loads(row["test_files_written"] or "[]"),
            summary=row["summary"] or "",
            notes=json.loads(row["notes"] or "[]"),
            tier_used=len(tiers) - 1 if tiers else 0,
            iterations_used=row["iterations_used"] or 0,
            last_test_output_tail=row["last_test_output"] or "",
        )
    return IssueOutcome(
        issue_id=row["issue_id"],
        disposition=row["status"],
        tiers_used=tiers,
        final_result=final_result,
        error=row["error"] or "",
    )
