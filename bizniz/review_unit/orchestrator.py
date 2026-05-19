"""Parallel review-unit orchestrator (Stage B).

Runs the review-class signal sources concurrently and aggregates
into a single ``FindingsReport`` for the BatchFixDebugger.

Stage B scope: QualityEngineer.review + CodeReviewer.review in parallel.
Today's pipeline already runs both of these — Stage B just runs them
concurrently instead of sequentially and normalizes their output via
the adapters. ``pytest`` and the deterministic static checks
(mypy/ruff/tsc) are extension points; their adapter modules sit
alongside under ``adapters/`` for incremental wiring.

Concurrency model: ``ThreadPoolExecutor`` with one worker per source.
Each source is an LLM subprocess (``claude --print``); they're
I/O-bound, so a thread pool is fine. (asyncio would work too; threads
are simpler given the existing synchronous client interface.)
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, List, Optional

from bizniz.architect.types import SystemArchitecture
from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.engineer.types import EngineerResult
from bizniz.quality_engineer.types import CoverageReport, EnrichedSpec
from bizniz.review_unit.adapters.code_reviewer import cr_report_to_findings
from bizniz.review_unit.adapters.quality_engineer import qe_coverage_to_findings
from bizniz.review_unit.types import FindingsReport, UnifiedFinding


class ReviewUnitOrchestrator:
    """Fan out review-class signal sources in parallel, aggregate into
    a unified ``FindingsReport``.

    Construction takes callable sources rather than concrete agents so
    callers can wire which LLM backend / model tier each source uses.
    Each callable should be parameterless and return the source's
    native result type (CoverageReport for QE, CodeReviewReport for CR).
    """

    def __init__(
        self,
        *,
        qe_review: Callable[[], CoverageReport],
        cr_review: Callable[[], CodeReviewReport],
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._qe_review = qe_review
        self._cr_review = cr_review
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def run(self, *, iteration: int = 0) -> FindingsReport:
        """Run QE + CR concurrently. Returns a unified findings report.

        Failures in individual sources are recorded as ``source_error``
        findings rather than crashing the whole review. The
        BatchFixDebugger can still make progress on whichever signals
        DID land.
        """
        self._log(
            f"ReviewUnitOrchestrator: starting iter {iteration} "
            f"(QE + CR in parallel)"
        )
        t0 = time.time()
        sources = {
            "quality_engineer": self._qe_review,
            "code_reviewer": self._cr_review,
        }
        raw_results: dict = {}
        errors: dict = {}

        with ThreadPoolExecutor(max_workers=len(sources)) as ex:
            future_to_name = {
                ex.submit(self._safe_call, fn): name
                for name, fn in sources.items()
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                ok, value = future.result()
                if ok:
                    raw_results[name] = value
                else:
                    errors[name] = value

        findings: List[UnifiedFinding] = []
        if "quality_engineer" in raw_results:
            qe_report = raw_results["quality_engineer"]
            findings.extend(qe_coverage_to_findings(qe_report))
        if "code_reviewer" in raw_results:
            cr_report = raw_results["code_reviewer"]
            findings.extend(cr_report_to_findings(cr_report))

        # Source failures become explicit findings so the debugger
        # sees that a signal stream was unavailable.
        for source, err in errors.items():
            findings.append(UnifiedFinding(
                source="quality_engineer" if source == "quality_engineer" else "code_reviewer",
                severity="medium",
                fingerprint=f"source_error.{source}",
                message=(
                    f"Review-unit source `{source}` failed during "
                    f"iteration {iteration}: {err}. The remaining "
                    f"sources may still have actionable findings."
                ),
            ))

        report = FindingsReport(iteration=iteration, findings=findings)
        elapsed = time.time() - t0
        self._log(
            f"ReviewUnitOrchestrator: iter {iteration} → "
            f"{report.summary_line()} in {elapsed:.1f}s"
        )
        return report

    @staticmethod
    def _safe_call(fn: Callable[[], Any]):
        """Wrap a source call in a (ok, value-or-error) tuple."""
        try:
            return (True, fn())
        except Exception as e:
            return (False, f"{type(e).__name__}: {e}")
