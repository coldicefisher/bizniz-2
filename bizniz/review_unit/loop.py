"""``ReviewUnitLoop`` — drives orchestrator + BatchFixDebugger to convergence.

The full Stage B loop:

  1. Run ``ReviewUnitOrchestrator`` (parallel QE + CR) → FindingsReport
  2. If findings empty → DONE, return ``passed`` verdict
  3. Else: hand findings to ``BatchFixDebugger`` → it applies fixes
  4. Re-run orchestrator → new FindingsReport
  5. ProgressTracker compares old vs new:
       - clean       → DONE, passed
       - progress    → loop continues, reset stall counter
       - stall/regress → stall counter++, escalate tier when threshold hit
  6. Hard cap on total iterations as a defensive bound

Stage B drops this loop in to replace today's sequential
``QE → CR → ServicePlanner.repair → Coder × N → loop`` pattern.
Same outer contract (eventually returns "milestone approved" or
"milestone halted"); inner mechanics are the parallel + batch-fix
architecture.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.review_unit.batch_fix_debugger import (
    BatchFixDebugger, BatchFixResult, compute_progress_verdict,
)
from bizniz.review_unit.orchestrator import ReviewUnitOrchestrator
from bizniz.review_unit.types import (
    FindingsReport, ProgressVerdict, UnifiedFinding,
)


# ── Loop result ───────────────────────────────────────────────────


class ReviewUnitLoopResult(BaseModel):
    """Final state from the review-unit loop."""

    approved: bool = Field(
        ...,
        description=(
            "True if the loop reached zero findings within the iteration "
            "cap. False if the loop exhausted iterations or escalation "
            "tiers without converging."
        ),
    )
    iterations: int = Field(default=0)
    final_findings: FindingsReport = Field(default_factory=lambda: FindingsReport())
    history: List[ProgressVerdict] = Field(default_factory=list)
    wall_s: float = Field(default=0.0)
    halt_reason: str = Field(
        default="",
        description="When ``approved=False``, why the loop bailed: stall|hard_cap|orchestrator_error.",
    )
    fixes_per_iteration: List[BatchFixResult] = Field(default_factory=list)


# ── Loop ──────────────────────────────────────────────────────────


class ReviewUnitLoop:
    """Run the v3 review unit to convergence (or stall escalation)."""

    def __init__(
        self,
        *,
        orchestrator: ReviewUnitOrchestrator,
        debugger_factory: Callable[[], BatchFixDebugger],
        workspace_root: Path,
        compose_path: Optional[str] = None,
        service_name: Optional[str] = None,
        stall_threshold: int = 5,
        hard_cap: int = 20,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._orchestrator = orchestrator
        self._debugger_factory = debugger_factory
        self._workspace_root = Path(workspace_root)
        self._compose_path = compose_path
        self._service_name = service_name
        self._stall_threshold = stall_threshold
        self._hard_cap = hard_cap
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def run(self) -> ReviewUnitLoopResult:
        """Drive the loop until clean or bail.

        Returns ``ReviewUnitLoopResult``. ``approved=True`` only when
        the final iteration produced zero findings. Stall escalation
        and hard cap both produce ``approved=False`` with the
        ``halt_reason`` field populated.
        """
        t0 = time.time()
        history: List[ProgressVerdict] = []
        fixes_per_iter: List[BatchFixResult] = []
        prior: Optional[FindingsReport] = None
        stall_counter = 0
        iteration = 0
        halt_reason = ""

        while iteration < self._hard_cap:
            # Step 1: run orchestrator.
            try:
                current = self._orchestrator.run(iteration=iteration)
            except Exception as e:
                self._log(
                    f"ReviewUnitLoop: iter {iteration} orchestrator "
                    f"raised: {type(e).__name__}: {e}"
                )
                halt_reason = f"orchestrator_error: {type(e).__name__}: {e}"
                break

            verdict = compute_progress_verdict(
                prior=prior,
                current=current,
                stall_counter=stall_counter,
                stall_threshold=self._stall_threshold,
            )
            history.append(verdict)

            self._log(
                f"ReviewUnitLoop: iter {iteration} {current.summary_line()} "
                f"verdict={verdict.verdict} stall={verdict.stall_counter}"
                f"/{verdict.stall_threshold}"
            )

            if verdict.verdict == "clean":
                wall = time.time() - t0
                return ReviewUnitLoopResult(
                    approved=True,
                    iterations=iteration + 1,
                    final_findings=current,
                    history=history,
                    wall_s=wall,
                    fixes_per_iteration=fixes_per_iter,
                )

            if not verdict.should_continue:
                halt_reason = (
                    f"stall_threshold_exceeded "
                    f"({verdict.stall_counter}/{verdict.stall_threshold})"
                )
                # Final state captured below.
                prior = current
                break

            # Step 2: hand findings to debugger; let it fix what it can.
            debugger = self._debugger_factory()
            try:
                fix_result = debugger.run(
                    report=current,
                    compose_path=self._compose_path,
                    service_name=self._service_name,
                )
                fixes_per_iter.append(fix_result)
                self._log(
                    f"ReviewUnitLoop: iter {iteration} debugger applied "
                    f"{len(fix_result.fixes_applied)} fix(es) in "
                    f"{fix_result.wall_s:.1f}s"
                )
            except Exception as e:
                self._log(
                    f"ReviewUnitLoop: iter {iteration} debugger raised: "
                    f"{type(e).__name__}: {e}"
                )
                halt_reason = f"debugger_error: {type(e).__name__}: {e}"
                prior = current
                break

            prior = current
            stall_counter = verdict.stall_counter
            iteration += 1

        # Fell through — either hard cap or break above.
        wall = time.time() - t0
        if not halt_reason:
            halt_reason = f"hard_cap ({self._hard_cap} iterations)"
        self._log(
            f"ReviewUnitLoop: halted after {iteration + 1} iter(s) "
            f"in {wall:.1f}s ({halt_reason})"
        )
        return ReviewUnitLoopResult(
            approved=False,
            iterations=iteration + 1,
            final_findings=prior or FindingsReport(),
            history=history,
            wall_s=wall,
            halt_reason=halt_reason,
            fixes_per_iteration=fixes_per_iter,
        )
