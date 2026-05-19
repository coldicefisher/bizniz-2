"""``PIRunner`` — parallel issue runner for v4.

Given a list of Issues, build a dependency DAG (file-overlap edges ∪
planner-emitted ``depends_on`` edges, additive-only union), Kahn's
topological sort into levels, and run each level concurrently via
``ThreadPoolExecutor`` capped at ``max_parallel`` workers.

A single callable — ``issue_runner: (Issue) -> ValidatedIssue`` —
encapsulates the per-issue work (CoderTesterAgent + PerIssueValidator).
PIRunner doesn't know about LLMs or scanners; it only orchestrates.

Strict level-by-level execution: when a level has fewer issues than
``max_parallel``, spare workers idle until the level finishes. No
speculative pickup from level N+1. (Decision locked in the v4 spec
2026-05-19.)

DAG correctness:
- file-overlap edge: if issue A and B both have a file in their
  ``target_files`` (or ``test_files``) and B's ``depends_on`` lists
  A's id, B → A. If neither lists the other, they share a file → we
  serialize by their list order (deterministic, conservative).
- planner ``depends_on``: any id in B.depends_on becomes a B ← that_id
  edge. Additive only — never removes a file-overlap edge.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from bizniz.coder.types import Issue
from bizniz.per_issue_validator.types import ValidatedIssue


# ── Run result ──────────────────────────────────────────────────────


class PIRunnerResult(BaseModel):
    """Final state from one PIRunner.run() invocation."""

    validated: List[ValidatedIssue] = Field(default_factory=list)
    levels: List[List[str]] = Field(
        default_factory=list,
        description=(
            "The DAG's topological levels (each list = issue ids that "
            "ran in parallel together)."
        ),
    )
    wall_s: float = 0.0
    max_parallel: int = 6
    failed_to_run: List[str] = Field(
        default_factory=list,
        description="Issue ids that never got dispatched (cycle in DAG).",
    )

    @property
    def clean_count(self) -> int:
        return sum(1 for v in self.validated if v.clean)

    @property
    def total_count(self) -> int:
        return len(self.validated)

    def summary_line(self) -> str:
        return (
            f"PIRunner: {self.clean_count}/{self.total_count} clean, "
            f"{len(self.levels)} level(s), {self.wall_s:.1f}s wall, "
            f"max_parallel={self.max_parallel}"
        )


# ── DAG builder ─────────────────────────────────────────────────────


def build_dag(issues: List[Issue]) -> Dict[str, Set[str]]:
    """Return adjacency: issue_id → set of issue_ids it depends on.

    Edges come from two sources, taken as a UNION (additive only):
    1. File overlap: any two issues sharing a file in
       ``target_files`` ∪ ``test_files``. The later-listed issue
       depends on the earlier. (Deterministic, list-order-based.)
    2. Planner-emitted ``depends_on``: any id in B.depends_on becomes
       an edge B → that_id. Additive only — file-overlap edges are
       never removed.

    Self-edges and references to unknown ids are dropped silently.
    Cycles are NOT broken here; ``topological_levels`` will surface
    unschedulable nodes via ``failed_to_run``.
    """
    by_id: Dict[str, Issue] = {i.id: i for i in issues}
    deps: Dict[str, Set[str]] = {i.id: set() for i in issues}

    # 1. File overlap edges. For each pair (A before B in list order),
    # if their file sets overlap, B depends on A.
    for a_idx, a in enumerate(issues):
        a_files = set(a.target_files) | set(a.test_files)
        if not a_files:
            continue
        for b in issues[a_idx + 1:]:
            b_files = set(b.target_files) | set(b.test_files)
            if a_files & b_files:
                deps[b.id].add(a.id)

    # 2. Planner depends_on edges (additive).
    for i in issues:
        for dep_id in (i.depends_on or []):
            if dep_id == i.id:
                continue  # self-edge: drop
            if dep_id not in by_id:
                continue  # unknown id: drop (don't fail loudly)
            deps[i.id].add(dep_id)

    return deps


# ── Topological levels (Kahn's) ────────────────────────────────────


def topological_levels(
    issues: List[Issue], deps: Dict[str, Set[str]],
) -> Tuple[List[List[str]], List[str]]:
    """Return (levels, unscheduled).

    Levels: each inner list = issue ids that have no outstanding deps
    once prior levels finish. Issues in the same level can run in
    parallel.

    Unscheduled: issue ids stuck in a cycle (deps never clear).
    """
    by_id: Dict[str, Issue] = {i.id: i for i in issues}
    remaining: Dict[str, Set[str]] = {k: set(v) for k, v in deps.items()}
    done: Set[str] = set()
    levels: List[List[str]] = []

    while True:
        ready = [
            i.id for i in issues
            if i.id not in done and not remaining[i.id]
        ]
        if not ready:
            break
        levels.append(ready)
        for r in ready:
            done.add(r)
        # Remove ``done`` ids from everyone else's deps.
        for k in remaining:
            remaining[k] -= done

    unscheduled = [i.id for i in issues if i.id not in done]
    return levels, unscheduled


# ── Runner ──────────────────────────────────────────────────────────


class PIRunner:
    """Parallel issue runner. Builds the DAG, runs each level
    concurrently up to ``max_parallel``, returns ValidatedIssue[].
    """

    def __init__(
        self,
        *,
        max_parallel: int = 6,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        if max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")
        self._max_parallel = int(max_parallel)
        self._on_status = on_status

    def run(
        self,
        *,
        issues: List[Issue],
        issue_runner: Callable[[Issue], ValidatedIssue],
    ) -> PIRunnerResult:
        """Run ``issue_runner`` on each issue, parallelized per the DAG.

        ``issue_runner`` is called with one Issue and returns the
        ValidatedIssue. Any exception raised by the runner is caught
        and converted to a ValidatedIssue(clean=False) so other parallel
        issues in the same level aren't blocked.
        """
        t0 = time.time()
        deps = build_dag(issues)
        levels, unscheduled = topological_levels(issues, deps)
        self._log(
            f"PIRunner: {len(issues)} issue(s) → {len(levels)} level(s) "
            f"({[len(l) for l in levels]}); max_parallel={self._max_parallel}"
        )
        if unscheduled:
            self._log(
                f"PIRunner: WARNING — {len(unscheduled)} unschedulable "
                f"issue(s) (cycle in DAG): {unscheduled}"
            )

        validated: List[ValidatedIssue] = []
        by_id: Dict[str, Issue] = {i.id: i for i in issues}

        for level_idx, level in enumerate(levels):
            self._log(
                f"PIRunner: starting level {level_idx + 1}/{len(levels)} "
                f"({len(level)} issue(s) in parallel): {level}"
            )
            level_t0 = time.time()
            workers = min(self._max_parallel, len(level))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(self._safe_run, by_id[iid], issue_runner): iid
                    for iid in level
                }
                for future in as_completed(futures):
                    iid = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        # _safe_run swallows exceptions; this branch
                        # is belt-and-suspenders.
                        result = ValidatedIssue(
                            issue_id=iid,
                            clean=False,
                            halt_reason=f"runner_exception: {type(e).__name__}: {e}",
                        )
                    validated.append(result)
                    self._log(
                        f"PIRunner: [{iid}] "
                        f"{'CLEAN' if result.clean else 'BROKEN'} "
                        f"({result.debug_iterations} debug iter(s)"
                        + (f", halt={result.halt_reason}" if not result.clean else "")
                        + ")"
                    )
            level_wall = time.time() - level_t0
            self._log(
                f"PIRunner: level {level_idx + 1} done in {level_wall:.1f}s"
            )

        for iid in unscheduled:
            validated.append(ValidatedIssue(
                issue_id=iid,
                clean=False,
                halt_reason="dag_cycle: never dispatched",
            ))

        wall = time.time() - t0
        out = PIRunnerResult(
            validated=validated,
            levels=levels,
            wall_s=wall,
            max_parallel=self._max_parallel,
            failed_to_run=unscheduled,
        )
        self._log(out.summary_line())
        return out

    @staticmethod
    def _safe_run(
        issue: Issue,
        issue_runner: Callable[[Issue], ValidatedIssue],
    ) -> ValidatedIssue:
        """Run one issue, converting any exception into a
        ValidatedIssue(clean=False) so one bad issue doesn't kill its
        level."""
        try:
            return issue_runner(issue)
        except Exception as e:
            return ValidatedIssue(
                issue_id=issue.id,
                clean=False,
                halt_reason=f"runner_exception: {type(e).__name__}: {e}",
            )

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass
