"""Progress-based stopping for iterative debug/recovery loops.

Replaces fixed-N iteration caps with a smarter rule: only stop
when the loop has stopped making headway, no matter how long it
takes. As long as failures keep going DOWN, the loop keeps
running.

The rule, per user direction (2026-05-17):

- **Progress** (failure count decreased) → reset stall counter
- **Stalled** (failure count flat) → stall counter += 1
- **Regression** (failure count went up) → stall counter += 1
- ``should_stop()`` true when stall counter >= threshold (default 5)

Why this beats fixed-N caps:
- A long, real diagnosis (10+ iters, gradually fixing things) gets
  the runway it needs.
- A genuinely stuck loop (no fixes landing) still stops cleanly.
- Single source of truth for "is the loop making headway?"

Used by:
- Integration debug loop (replaces ``max_iterations``)
- Smoke recovery (replaces single-shot)
- Post-refactor test phase (replaces tier-list cap)
- FinalTest recovery (when wired)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal

Verdict = Literal["progress", "stalled", "regression"]


@dataclass
class ProgressTracker:
    """Tracks failure count across iterations + decides when to stop.

    Construct with the initial failure count; call ``update`` with
    the failure count after each iteration. ``should_stop`` reports
    whether the loop should terminate.

    ``stall_threshold`` is the number of consecutive non-progress
    iterations (stalled + regression combined) before stopping.
    Default 5 matches ``BiznizConfig.debugger_stall_threshold``.
    """
    initial_failure_count: int
    stall_threshold: int = 5

    # State.
    _current_failure_count: int = field(init=False)
    _consecutive_no_progress: int = field(init=False, default=0)
    _history: List["IterationOutcome"] = field(
        init=False, default_factory=list,
    )

    def __post_init__(self) -> None:
        self._current_failure_count = self.initial_failure_count

    @property
    def current_failure_count(self) -> int:
        return self._current_failure_count

    @property
    def consecutive_no_progress(self) -> int:
        return self._consecutive_no_progress

    @property
    def history(self) -> List["IterationOutcome"]:
        return list(self._history)

    def update(self, new_failure_count: int) -> Verdict:
        """Record one iteration's outcome + return the verdict.

        Verdicts:
        - ``"progress"`` — failures went down; reset stall counter
        - ``"stalled"`` — failures flat; stall counter += 1
        - ``"regression"`` — failures went up; stall counter += 1
        """
        prev = self._current_failure_count
        delta = new_failure_count - prev
        if delta < 0:
            verdict: Verdict = "progress"
            self._consecutive_no_progress = 0
        elif delta > 0:
            verdict = "regression"
            self._consecutive_no_progress += 1
        else:
            verdict = "stalled"
            self._consecutive_no_progress += 1
        self._current_failure_count = new_failure_count
        self._history.append(IterationOutcome(
            iteration_index=len(self._history) + 1,
            failure_count_before=prev,
            failure_count_after=new_failure_count,
            verdict=verdict,
            stall_counter_after=self._consecutive_no_progress,
        ))
        return verdict

    def should_stop(self) -> bool:
        return self._consecutive_no_progress >= self.stall_threshold

    def has_converged(self) -> bool:
        """True when every failure has been fixed (count == 0)."""
        return self._current_failure_count == 0

    def render_history(self) -> str:
        """Short table for log + post-mortem reports."""
        lines = [
            f"iter | failures (before → after) | verdict | stall count",
            f"-----+---------------------------+---------+------------",
        ]
        for h in self._history:
            arrow = (
                "↓" if h.verdict == "progress"
                else "↑" if h.verdict == "regression"
                else "="
            )
            lines.append(
                f"{h.iteration_index:>4} | "
                f"{h.failure_count_before:>3} → {h.failure_count_after:<3} {arrow} | "
                f"{h.verdict:<10}| {h.stall_counter_after}"
            )
        return "\n".join(lines)


@dataclass
class IterationOutcome:
    """Per-iteration record kept by the tracker."""
    iteration_index: int
    failure_count_before: int
    failure_count_after: int
    verdict: Verdict
    stall_counter_after: int
