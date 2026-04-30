"""
:class:`CostTracker` — collects per-call usage records and rolls them up.

A module-level singleton (``get_tracker()``) is the default destination
that AI clients write to after every call. Tests can use ``set_tracker``
or instantiate fresh trackers. Optionally a tracker can persist each
record to a workspace SQLite database for cross-run analysis.
"""
from __future__ import annotations

import datetime
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from bizniz.cost.pricing import CallCost, price_call


@dataclass
class CallRecord:
    """One AI call's worth of cost + usage data."""
    timestamp: str
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    cost: CallCost
    problem_id: Optional[int] = None
    issue_id: Optional[int] = None


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
    """Thread-safe in-memory cost log with optional DB persistence."""

    def __init__(
        self,
        workspace_db=None,
        problem_id: Optional[int] = None,
        issue_id: Optional[int] = None,
    ):
        self._lock = threading.Lock()
        self._records: List[CallRecord] = []
        self._workspace_db = workspace_db
        self._problem_id = problem_id
        self._issue_id = issue_id

    def attach_workspace_db(self, workspace_db) -> None:
        """Bind a workspace DB; subsequent records also persist there."""
        with self._lock:
            self._workspace_db = workspace_db

    def set_context(
        self,
        problem_id: Optional[int] = None,
        issue_id: Optional[int] = None,
    ) -> None:
        """Set the problem/issue identifiers attached to subsequent records."""
        with self._lock:
            if problem_id is not None:
                self._problem_id = problem_id
            if issue_id is not None:
                self._issue_id = issue_id

    def record(
        self,
        agent: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int = 0,
        problem_id: Optional[int] = None,
        issue_id: Optional[int] = None,
    ) -> CallRecord:
        """Record one AI call. Cost is computed from the pricing table."""
        cost = price_call(model, input_tokens, output_tokens)
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
        )
        with self._lock:
            self._records.append(rec)
            if self._workspace_db is not None:
                try:
                    self._workspace_db.save_api_call(rec)
                except Exception:
                    # Persistence is best-effort; never break a real call.
                    pass
        return rec

    def reset(self) -> None:
        """Drop all records (useful between tests / runs)."""
        with self._lock:
            self._records = []

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
