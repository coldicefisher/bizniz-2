"""
:class:`CostTracker` — collects per-call usage records and rolls them up.

A module-level singleton (``get_tracker()``) is the default destination
that AI clients write to after every call. Tests can use ``set_tracker``
or instantiate fresh trackers. Optionally a tracker can persist each
record to the project SQLite database (``ProjectDB``) for cross-run
analysis.

Job model
---------

A *job* is one ``architect.build()`` invocation (or any other top-level
unit of work). Call ``tracker.start_job(project_slug, problem_statement)``
at the beginning to allocate a job_id; every subsequent ``record()``
attaches that id. Mid-job, push context with ``set_service()``,
``set_issue()``, ``set_phase()`` so each call lands in the right bucket
for rollups (``cost_by_issue``, ``cost_by_service``, ``cost_by_model``).

Records buffered before ``attach_project_db()`` (e.g. the architect's
decompose call before the project even exists) are flushed when the DB
is attached, so no calls are dropped.
"""
from __future__ import annotations

import datetime
import threading
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from bizniz.cost.pricing import CallCost, price_call


@dataclass
class CallRecord:
    """One AI call's worth of cost + usage data.

    ``image_count`` is the number of images generated (or 0 for text-only
    calls). Image-generation models (e.g. ``gemini-3-pro-image-preview``)
    bill per image in addition to the usual token I/O; the cost is
    folded into ``cost.image_cost`` and ``cost.total_cost``.
    """
    timestamp: str
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    cost: CallCost
    problem_id: Optional[int] = None
    issue_id: Optional[int] = None
    job_id: Optional[str] = None
    service_name: Optional[str] = None
    phase: Optional[str] = None
    milestone_id: Optional[int] = None
    image_count: int = 0
    cached_input_tokens: int = 0


@dataclass
class CostSummary:
    """Aggregate roll-up across all recorded calls."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0.0
    by_model: Dict[str, Dict[str, float]] = field(default_factory=dict)
    by_agent: Dict[str, Dict[str, float]] = field(default_factory=dict)
    unpriced_calls: int = 0
    unpriced_models: List[str] = field(default_factory=list)

    def format(self) -> str:
        """Human-readable single-string summary (for logs / docs)."""
        lines = [
            f"calls={self.calls}  "
            f"input={self.input_tokens:,}  "
            f"output={self.output_tokens:,}  "
            f"total=${self.total_cost:.4f}",
        ]
        if self.by_model:
            lines.append("  by model:")
            for model, m in sorted(self.by_model.items()):
                lines.append(
                    f"    {model:42s}  calls={m['calls']:>3.0f}  "
                    f"in={m['input_tokens']:>8,.0f}  out={m['output_tokens']:>8,.0f}  "
                    f"${m['cost']:.4f}"
                )
        if self.by_agent:
            lines.append("  by agent:")
            for agent, a in sorted(self.by_agent.items()):
                lines.append(
                    f"    {agent:25s}  calls={a['calls']:>3.0f}  ${a['cost']:.4f}"
                )
        if self.unpriced_calls:
            lines.append(
                f"  WARNING: {self.unpriced_calls} call(s) had no pricing entry "
                f"(models: {sorted(set(self.unpriced_models))}). "
                f"Add them to bizniz/cost/pricing.py to include in totals."
            )
        return "\n".join(lines)


class CostTracker:
    """Thread-safe in-memory cost log with optional ProjectDB persistence.

    Lifecycle::

        tracker = get_tracker()
        tracker.start_job(project_slug, problem_statement)   # allocates job_id
        # ... some calls happen via clients ...
        tracker.attach_project_db(project.db)                # flushes buffered + live persists
        tracker.set_service("backend")
        tracker.set_issue(issue.db_id)
        tracker.set_phase("frame")
        # ... more calls ...
        tracker.finish_job(status="succeeded")                # rolls up totals
    """

    def __init__(
        self,
        project_db=None,
        problem_id: Optional[int] = None,
        issue_id: Optional[int] = None,
    ):
        self._lock = threading.Lock()
        self._records: List[CallRecord] = []
        self._project_db = project_db
        # Per-call context (set by callers before invoking AI clients)
        self._problem_id = problem_id
        self._issue_id = issue_id
        self._job_id: Optional[str] = None
        self._service_name: Optional[str] = None
        self._phase: Optional[str] = None
        self._milestone_id: Optional[int] = None
        # Records that arrived before a DB was attached are flushed when one
        # is. The set tracks rows already persisted so re-attach doesn't
        # double-write.
        self._persisted_record_ids: set = set()

    # ── Compatibility shim for older callers ─────────────────────────────────

    @property
    def workspace_db(self):  # legacy alias kept for old tests
        return self._project_db

    def attach_workspace_db(self, db) -> None:
        """Deprecated alias for ``attach_project_db``. Kept for backward
        compatibility with any caller still using the older name."""
        self.attach_project_db(db)

    def attach_project_db(self, project_db) -> None:
        """Bind a ProjectDB; flush any buffered records and live-persist
        all subsequent records to it."""
        with self._lock:
            self._project_db = project_db
            for i, rec in enumerate(self._records):
                if i in self._persisted_record_ids:
                    continue
                try:
                    project_db.save_api_call(rec)
                    self._persisted_record_ids.add(i)
                except Exception:
                    pass

    # ── Job lifecycle ────────────────────────────────────────────────────────

    def start_job(
        self,
        project_slug: str,
        problem_statement: str = "",
        metadata: Optional[dict] = None,
    ) -> str:
        """Allocate a UUID job_id for this run. Subsequent records carry
        it. If a project_db is already attached, also write the job row.
        """
        with self._lock:
            self._job_id = str(uuid.uuid4())
            db = self._project_db
        if db is not None:
            try:
                db.start_job(
                    job_id=self._job_id,
                    project_slug=project_slug,
                    problem_statement=problem_statement,
                    metadata=metadata,
                )
            except Exception:
                pass
        # Stash for later DB attach
        self._pending_job_meta = (project_slug, problem_statement, metadata)
        return self._job_id

    def finish_job(self, status: str = "succeeded") -> None:
        """Mark the current job done. Refreshes the rollup totals on the
        jobs row from api_calls."""
        with self._lock:
            job_id = self._job_id
            db = self._project_db
        if not job_id or db is None:
            return
        try:
            db.finish_job(job_id, status=status)
        except Exception:
            pass

    # ── Context ──────────────────────────────────────────────────────────────

    def set_context(
        self,
        problem_id: Optional[int] = None,
        issue_id: Optional[int] = None,
        service_name: Optional[str] = None,
        phase: Optional[str] = None,
        milestone_id: Optional[int] = None,
    ) -> None:
        """Set any/all per-call context fields at once. Pass None to leave
        a field unchanged."""
        with self._lock:
            if problem_id is not None:
                self._problem_id = problem_id
            if issue_id is not None:
                self._issue_id = issue_id
            if service_name is not None:
                self._service_name = service_name
            if phase is not None:
                self._phase = phase
            if milestone_id is not None:
                self._milestone_id = milestone_id

    def set_service(self, service_name: Optional[str]) -> None:
        with self._lock:
            self._service_name = service_name

    def set_issue(self, issue_id: Optional[int]) -> None:
        with self._lock:
            self._issue_id = issue_id

    def set_phase(self, phase: Optional[str]) -> None:
        with self._lock:
            self._phase = phase

    def set_milestone(self, milestone_id: Optional[int]) -> None:
        """Tag subsequent records with this milestone_id. Used by future
        evolve-mode runs that step the project through a Planner-produced
        sequence of milestones; current architect.build() runs leave
        this unset (NULL milestone_id rows)."""
        with self._lock:
            self._milestone_id = milestone_id

    @property
    def current_job_id(self) -> Optional[str]:
        return self._job_id

    # ── Recording ────────────────────────────────────────────────────────────

    def record(
        self,
        agent: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int = 0,
        problem_id: Optional[int] = None,
        issue_id: Optional[int] = None,
        service_name: Optional[str] = None,
        phase: Optional[str] = None,
        milestone_id: Optional[int] = None,
        image_count: int = 0,
        cached_input_tokens: int = 0,
    ) -> CallRecord:
        """Record one AI call. Cost is computed from the pricing table.

        ``image_count`` is the number of images generated by this call
        (default 0). For models with an ``image`` price entry, this
        contributes to ``cost.image_cost`` and ``cost.total_cost``.

        ``cached_input_tokens`` is the portion of ``input_tokens``
        served from a provider-side prompt cache. Discounted at 25%
        of the normal input rate (Gemini 2.5+ default). Caller still
        passes the GROSS ``input_tokens`` (cached + uncached); this
        method does the discount math.
        """
        cost = price_call(
            model, input_tokens, output_tokens,
            image_count=image_count,
            cached_input_tokens=cached_input_tokens,
        )
        rec = CallRecord(
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            agent=agent,
            model=model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            duration_ms=int(duration_ms or 0),
            cost=cost,
            problem_id=problem_id if problem_id is not None else self._problem_id,
            issue_id=issue_id if issue_id is not None else self._issue_id,
            job_id=self._job_id,
            service_name=(
                service_name if service_name is not None else self._service_name
            ),
            phase=phase if phase is not None else self._phase,
            milestone_id=(
                milestone_id if milestone_id is not None else self._milestone_id
            ),
            image_count=int(image_count or 0),
            cached_input_tokens=int(cached_input_tokens or 0),
        )
        with self._lock:
            idx = len(self._records)
            self._records.append(rec)
            db = self._project_db
        if db is not None:
            try:
                db.save_api_call(rec)
                self._persisted_record_ids.add(idx)
            except Exception:
                # Persistence is best-effort; never break a real call.
                pass
        return rec

    def reset(self) -> None:
        """Drop all records (useful between tests / runs)."""
        with self._lock:
            self._records = []
            self._persisted_record_ids = set()
            self._job_id = None
            self._service_name = None
            self._phase = None
            self._milestone_id = None

    def records(self) -> List[CallRecord]:
        with self._lock:
            return list(self._records)

    def summary(self) -> CostSummary:
        """Roll the records up into a :class:`CostSummary`."""
        with self._lock:
            recs = list(self._records)

        s = CostSummary()
        s.calls = len(recs)
        for r in recs:
            s.input_tokens += r.input_tokens
            s.output_tokens += r.output_tokens
            s.total_cost += r.cost.total_cost

            m = s.by_model.setdefault(r.cost.model, {
                "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0,
            })
            m["calls"] += 1
            m["input_tokens"] += r.input_tokens
            m["output_tokens"] += r.output_tokens
            m["cost"] += r.cost.total_cost

            a = s.by_agent.setdefault(r.agent, {
                "calls": 0, "cost": 0.0,
            })
            a["calls"] += 1
            a["cost"] += r.cost.total_cost

            if not r.cost.priced:
                s.unpriced_calls += 1
                s.unpriced_models.append(r.model)

        return s


# ── Module-level default tracker ──────────────────────────────────────────────

_default_tracker = CostTracker()


def get_tracker() -> CostTracker:
    """Return the process-global tracker. Clients write here by default."""
    return _default_tracker


def set_tracker(tracker: CostTracker) -> None:
    """Replace the process-global tracker (mostly for tests)."""
    global _default_tracker
    _default_tracker = tracker
