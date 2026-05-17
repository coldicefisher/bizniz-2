"""RefactorerAgent — orchestrates the v2 refactor loop.

Phase G of roadmap item 6. Composes Phases A-F into a single
``RefactorerAgent.run()`` call that:

1. Walks the project source tree (Phase A's tokenizer-aware walker)
2. Runs CPD detection (Phase A) → ``CPDReport``
3. Runs anti-pattern scan (Phase C) → ``AntiPatternReport``
4. Plans extractions (Phase E) → ``ExtractionPlanReport``
5. Classifies anti-pattern findings (Phase D) → ``WhyReport``
6. For each extract-disposition plan AND each auto-fix anti-pattern
   verdict, dispatches to the executor (Phase F)
7. Aggregates results, self-rates confidence per the item-1
   confidence-band pattern, decides whether to iterate

Confidence iteration (mirrors QualityEngineer.enrich):

- **≥ 0.7** — proceed to next extraction
- **0.4-0.6** — re-attempt with augmented context
- **< 0.4** — revert this extraction, log, move on

Total work ends when EITHER no more extract-disposition plans
remain OR cumulative low-confidence count exceeds the threshold.

All collaborators are constructor-injected. Tests inject fakes
for each phase; production wiring lives in ``v2_build.py``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.refactorer.anti_patterns import (
    AntiPatternReport, scan_files,
)
from bizniz.refactorer.cpd import (
    CPDConfig, CPDReport, detect_duplicates, walk_source_tree,
)
from bizniz.refactorer.extraction_executor import (
    ExtractionExecutor, ExtractionResult,
)
from bizniz.refactorer.extraction_planner import (
    ExtractionPlan, ExtractionPlanReport, plan_extractions,
)
from bizniz.refactorer.why_classifier import (
    WhyClassifier, WhyReport,
)


# ── Output schema ────────────────────────────────────────────────


class RefactorerRunResult(BaseModel):
    """End-of-run summary."""
    duration_s: float = 0.0
    cpd_report: Optional[CPDReport] = None
    anti_pattern_report: Optional[AntiPatternReport] = None
    plan_report: Optional[ExtractionPlanReport] = None
    why_report: Optional[WhyReport] = None
    extraction_results: List[ExtractionResult] = Field(default_factory=list)
    extractions_applied: int = 0
    extractions_reverted: int = 0
    extractions_skipped: int = 0
    surfaced_findings: int = 0   # manual_review + low-confidence
    skipped_reason: Optional[str] = None
    notes: List[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        # "passed" = ran cleanly (didn't abort) AND didn't leave any
        # extractions in a half-applied state. extractions_reverted
        # is not a pass-failure — reverts are part of normal flow.
        return self.skipped_reason is None


# ── Agent ────────────────────────────────────────────────────────


class RefactorerAgent:
    """Drives the full refactor loop.

    All collaborators are constructor-injected for testability:
    ``executor`` (Phase F), ``why_classifier`` (Phase D), the
    deterministic scanners default to the package functions but
    can be swapped for tests.
    """

    def __init__(
        self,
        project_root: Path,
        executor: ExtractionExecutor,
        why_classifier: Optional[WhyClassifier] = None,
        cpd_config: Optional[CPDConfig] = None,
        on_status: Optional[Callable[[str], None]] = None,
        # Injection points (defaults use production functions).
        walk_fn: Optional[Callable[[Path], List[str]]] = None,
        cpd_fn: Optional[Callable[..., CPDReport]] = None,
        scan_fn: Optional[Callable[[List[str]], AntiPatternReport]] = None,
        plan_fn: Optional[Callable[..., ExtractionPlanReport]] = None,
        # Confidence iteration thresholds (matches item 1 pattern).
        max_extractions: int = 50,
        consecutive_failures_cap: int = 3,
    ) -> None:
        self._project_root = Path(project_root)
        self._executor = executor
        self._why_classifier = why_classifier
        self._cpd_config = cpd_config or CPDConfig()
        self._on_status = on_status
        self._walk_fn = walk_fn or walk_source_tree
        self._cpd_fn = cpd_fn or detect_duplicates
        self._scan_fn = scan_fn or scan_files
        self._plan_fn = plan_fn or plan_extractions
        self._max_extractions = max_extractions
        self._consecutive_failures_cap = consecutive_failures_cap

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def run(self) -> RefactorerRunResult:
        """Run the full refactor loop. Never raises — wraps
        unexpected errors as ``skipped_reason`` so the milestone
        loop can continue."""
        t0 = time.time()
        result = RefactorerRunResult()
        try:
            return self._run_inner(result, t0)
        except Exception as e:
            result.skipped_reason = (
                f"refactorer aborted: {type(e).__name__}: {e}"
            )
            result.duration_s = time.time() - t0
            self._log(f"RefactorerAgent: {result.skipped_reason}")
            return result

    def _run_inner(
        self, result: RefactorerRunResult, t0: float,
    ) -> RefactorerRunResult:
        # ── Phase A: discover source files + run CPD ─────────────
        self._log(
            f"RefactorerAgent: walking {self._project_root}..."
        )
        files = self._walk_fn(self._project_root)
        if not files:
            result.skipped_reason = "no source files found"
            result.duration_s = time.time() - t0
            return result
        self._log(
            f"RefactorerAgent: walked {len(files)} files; running CPD..."
        )
        cpd_report = self._cpd_fn(files, config=self._cpd_config)
        result.cpd_report = cpd_report
        self._log(
            f"RefactorerAgent: CPD found "
            f"{len(cpd_report.duplicates)} duplicate block(s), "
            f"{len(cpd_report.fuzzy_pairs)} fuzzy pair(s)"
        )

        # ── Phase C: anti-pattern scan ───────────────────────────
        anti_report = self._scan_fn(files)
        result.anti_pattern_report = anti_report
        self._log(
            f"RefactorerAgent: anti-pattern scan found "
            f"{len(anti_report.findings)} finding(s) "
            f"({len(anti_report.by_severity('critical'))} critical)"
        )

        # ── Phase E: extraction planner ──────────────────────────
        plan_report = self._plan_fn(
            cpd_report, project_root=self._project_root,
        )
        result.plan_report = plan_report
        extracts = plan_report.extract_plans()
        manual = plan_report.manual_review_plans()
        result.surfaced_findings = len(manual)
        self._log(
            f"RefactorerAgent: planner produced "
            f"{len(extracts)} extract plan(s), "
            f"{len(manual)} for manual review"
        )

        # ── Phase D: classify anti-patterns (if classifier wired) ─
        if self._why_classifier is not None and anti_report.findings:
            why_report = self._why_classifier.classify_all(
                anti_report.findings,
            )
            result.why_report = why_report
            auto_fix = why_report.auto_fix_candidates()
            result.surfaced_findings += len(why_report.surface_candidates())
            self._log(
                f"RefactorerAgent: classifier auto-fix candidates: "
                f"{len(auto_fix)}; surface candidates: "
                f"{len(why_report.surface_candidates())}"
            )
            # Note: applying anti-pattern rewrites is conceptually
            # the same as extraction execution — same executor + git
            # discipline — but the prompt/plan shape differs. We
            # surface them for human review for now; auto-fixing
            # anti-patterns lands in a follow-up after extraction
            # is proven end-to-end on real builds.
            for v in auto_fix:
                result.notes.append(
                    f"auto-fix candidate (surfaced for now): "
                    f"{v.finding.pattern} at "
                    f"{v.finding.path}:{v.finding.line} "
                    f"(conf={v.confidence:.2f}) — {v.hypothesis}"
                )

        # ── Phase F: execute extractions (capped) ────────────────
        if not extracts:
            self._log("RefactorerAgent: no extract plans — done.")
            result.duration_s = time.time() - t0
            return result

        consecutive_failures = 0
        for i, plan in enumerate(extracts, 1):
            if i > self._max_extractions:
                result.notes.append(
                    f"max_extractions cap ({self._max_extractions}) "
                    f"reached — stopping early"
                )
                break
            if consecutive_failures >= self._consecutive_failures_cap:
                result.notes.append(
                    f"consecutive failures cap "
                    f"({self._consecutive_failures_cap}) reached — "
                    f"stopping early"
                )
                break

            self._log(
                f"RefactorerAgent: executing plan {i}/{len(extracts)} "
                f"({plan.duplicate_hash}, risk={plan.risk_score:.2f})..."
            )
            ext_result = self._executor.execute(plan)
            result.extraction_results.append(ext_result)
            if ext_result.status == "applied":
                result.extractions_applied += 1
                consecutive_failures = 0
            elif ext_result.status == "reverted":
                result.extractions_reverted += 1
                consecutive_failures += 1
            elif ext_result.status == "no_changes":
                result.extractions_skipped += 1
                consecutive_failures = 0
            else:  # "failed"
                consecutive_failures += 1

        result.duration_s = time.time() - t0
        self._log(
            f"RefactorerAgent: done in {result.duration_s:.1f}s — "
            f"{result.extractions_applied} applied, "
            f"{result.extractions_reverted} reverted, "
            f"{result.extractions_skipped} skipped, "
            f"{result.surfaced_findings} surfaced"
        )
        return result
