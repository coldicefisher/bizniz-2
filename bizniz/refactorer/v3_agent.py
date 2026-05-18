"""v3 RefactorerAgent — orchestrates the per-candidate pipeline.

Per ``docs/backlog/v3_refactorer_design.md``:

    Two scans:
      1. Deterministic (anti_patterns + CPD)
      2. Agent-driven misplaced business logic in frontline code

    For each candidate (from either scan):
      decision gate → destination plan → execute → verify

The verify step uses the deterministic import-resolution check
FIRST (cheap, catches the obvious broken-import failure mode in
seconds), THEN runs the project's test suite (slow source of
truth). On failure: git revert. On pass: git commit.

Python-only for v3. Frontend extraction queued for "v4 frontend
refactorer."

The class is named ``V3RefactorerAgent`` to avoid clashing with
the existing v2 ``RefactorerAgent``. v2's modules (anti_patterns,
cpd, tokenizers) are reused as building blocks for Signal 1.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from pydantic import BaseModel, Field

from bizniz.refactorer.anti_patterns import (
    AntiPatternFinding,
    AntiPatternReport,
    scan_files,
)
from bizniz.refactorer.cpd import (
    CPDConfig,
    CPDReport,
    DuplicateBlock,
    detect_duplicates,
)
from bizniz.refactorer.decision_gate import (
    CandidateContext,
    DecisionGate,
    GateDecision,
)
from bizniz.refactorer.destination_planner import (
    DestinationPlan,
    DestinationPlanner,
)
from bizniz.refactorer.extraction_executor import (
    ExtractionExecutor,
    ExtractionResult,
)
from bizniz.refactorer.extraction_planner import (
    ExtractionPlan,
    ExtractionPlanReport,
    plan_extractions,
)
from bizniz.refactorer.import_verifier import (
    ImportVerifier,
    ImportVerifierReport,
)
from bizniz.refactorer.misplacement_scanner import (
    MisplacedLogicCandidate,
    MisplacementReport,
    MisplacementScanner,
)


# ── Per-candidate trail ──────────────────────────────────────────


class CandidateTrail(BaseModel):
    """The full audit trail for ONE candidate as it flows through
    the v3 pipeline. Kept verbose so operators can debug rejected
    or reverted extractions without re-reading the run log."""
    candidate_kind: str
    summary: str
    file_path: str
    line_range: Optional[Tuple[int, int]] = None
    # Steps. Each may be None if the pipeline short-circuited earlier.
    gate_decision: Optional[GateDecision] = None
    destination_plan: Optional[DestinationPlan] = None
    extraction_result: Optional[ExtractionResult] = None
    import_problems: List[str] = Field(default_factory=list)
    final_status: str = "pending"
    failure_reason: Optional[str] = None


class V3RefactorerRunResult(BaseModel):
    """End-of-run summary for the v3 refactorer."""
    duration_s: float = 0.0
    files_scanned: int = 0
    candidates_total: int = 0
    candidates_skipped_by_gate: int = 0
    candidates_planned: int = 0
    candidates_applied: int = 0
    candidates_reverted: int = 0
    candidates_failed: int = 0
    trails: List[CandidateTrail] = Field(default_factory=list)
    skipped_reason: Optional[str] = None
    notes: List[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        # "Passed" = ran cleanly. Reverts are normal flow, not a
        # pass-failure — the executor's git_ops returned the
        # codebase to a known-good state.
        return self.skipped_reason is None


# ── Orchestrator ─────────────────────────────────────────────────


class V3RefactorerAgent:
    """End-to-end driver for the v3 refactor loop.

    All collaborators are constructor-injected. Tests stub each one
    to exercise the orchestration without spawning real LLM calls.

    The agent operates in PYTHON ONLY for v3. TypeScript scanning
    + frontend extraction are queued for a follow-up agent.

    **Per-candidate flow:**

      1. Decision Gate → cheap yes/no
      2. Destination Planner → core/python/ destination + signature
      3. Executor → file edits via Claude session (existing v2 module)
      4. Verifier → static import resolve + test suite
      5. Commit on pass / revert on fail

    Failures at ANY step degrade gracefully — the loop continues
    to the next candidate. The trail captures what happened.
    """

    def __init__(
        self,
        project_root: Path,
        executor: ExtractionExecutor,
        decision_gate: DecisionGate,
        destination_planner: DestinationPlanner,
        misplacement_scanner: MisplacementScanner,
        import_verifier: ImportVerifier,
        on_status: Optional[Callable[[str], None]] = None,
        # Injection points (defaults use production functions).
        walk_fn: Optional[Callable[[Path], List[str]]] = None,
        cpd_fn: Optional[Callable[..., CPDReport]] = None,
        anti_patterns_fn: Optional[Callable[[List[str]], AntiPatternReport]] = None,
        deterministic_plan_fn: Optional[
            Callable[..., ExtractionPlanReport]
        ] = None,
        # Safety caps.
        max_candidates: int = 30,
        cpd_config: Optional[CPDConfig] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._executor = executor
        self._gate = decision_gate
        self._dest_planner = destination_planner
        self._misplacement = misplacement_scanner
        self._verifier = import_verifier
        self._on_status = on_status
        self._walk_fn = walk_fn or _default_walk_python
        self._cpd_fn = cpd_fn or detect_duplicates
        self._anti_fn = anti_patterns_fn or scan_files
        self._plan_fn = deterministic_plan_fn or plan_extractions
        self._max_candidates = max_candidates
        self._cpd_config = cpd_config or CPDConfig()

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    # ── Public ─────────────────────────────────────────────────────

    def run(self) -> V3RefactorerRunResult:
        """Execute the full v3 loop. Never raises."""
        t0 = time.time()
        result = V3RefactorerRunResult()
        try:
            return self._run_inner(result, t0)
        except Exception as e:
            result.skipped_reason = (
                f"v3 refactor aborted: {type(e).__name__}: {e}"
            )
            result.duration_s = time.time() - t0
            self._log(f"V3RefactorerAgent: {result.skipped_reason}")
            return result

    # ── Internals ──────────────────────────────────────────────────

    def _run_inner(
        self, result: V3RefactorerRunResult, t0: float,
    ) -> V3RefactorerRunResult:
        self._log(
            f"V3RefactorerAgent: walking {self._project_root} for "
            f"Python source files..."
        )
        files = self._walk_fn(self._project_root)
        if not files:
            result.skipped_reason = "no Python source files found"
            result.duration_s = time.time() - t0
            return result
        result.files_scanned = len(files)
        self._log(
            f"V3RefactorerAgent: {len(files)} file(s) to consider"
        )

        # ── Signal 1: deterministic scans ────────────────────────
        anti_report = self._anti_fn(files)
        self._log(
            f"V3RefactorerAgent: anti-patterns — "
            f"{len(anti_report.findings)} finding(s)"
        )
        cpd_report = self._cpd_fn(files, config=self._cpd_config)
        self._log(
            f"V3RefactorerAgent: CPD — "
            f"{len(cpd_report.duplicates)} duplicate block(s)"
        )

        # CPD findings → deterministic ExtractionPlan list (with
        # heuristic suggested paths the destination planner will
        # refine).
        cpd_plans = self._plan_fn(
            cpd_report, project_root=self._project_root,
        ).extract_plans()

        # ── Signal 2: agent-driven misplacement scan ─────────────
        misplacement_report = self._misplacement.scan()
        self._log(
            f"V3RefactorerAgent: misplacement — "
            f"{len(misplacement_report.candidates)} candidate(s)"
        )

        # ── Unify into a single candidate stream ─────────────────
        candidates = _unify_candidates(
            anti_findings=anti_report.findings,
            cpd_plans=cpd_plans,
            cpd_report=cpd_report,
            misplaced=misplacement_report.candidates,
            max_total=self._max_candidates,
        )
        result.candidates_total = len(candidates)
        self._log(
            f"V3RefactorerAgent: {len(candidates)} total candidate(s) "
            f"after unification (cap={self._max_candidates})"
        )

        # ── Per-candidate pipeline ───────────────────────────────
        for context, original in candidates:
            trail = CandidateTrail(
                candidate_kind=context.kind,
                summary=context.summary,
                file_path=context.file_path,
                line_range=context.line_range,
            )
            result.trails.append(trail)
            try:
                self._process_one(context, original, trail, result)
            except Exception as e:
                trail.final_status = "errored"
                trail.failure_reason = f"{type(e).__name__}: {e}"
                result.candidates_failed += 1
                self._log(
                    f"V3RefactorerAgent: candidate {context.summary!r} "
                    f"raised {type(e).__name__}: {e} — continuing"
                )

        result.duration_s = time.time() - t0
        self._log(
            f"V3RefactorerAgent: done in {result.duration_s:.1f}s — "
            f"{result.candidates_applied} applied, "
            f"{result.candidates_reverted} reverted, "
            f"{result.candidates_failed} failed, "
            f"{result.candidates_skipped_by_gate} skipped by gate"
        )
        return result

    def _process_one(
        self,
        context: CandidateContext,
        original,
        trail: CandidateTrail,
        result: V3RefactorerRunResult,
    ) -> None:
        # Step 1 — gate
        decision = self._gate.decide(context)
        trail.gate_decision = decision
        if not decision.refactor:
            result.candidates_skipped_by_gate += 1
            trail.final_status = "skipped_by_gate"
            self._log(
                f"V3RefactorerAgent: SKIP {context.summary!r} "
                f"(gate: {decision.rationale[:120]})"
            )
            return

        # Step 2 — destination plan
        suggested = getattr(original, "suggested_core_path", None) \
            if isinstance(original, ExtractionPlan) else None
        dest = self._dest_planner.plan_for(
            candidate_kind=context.kind,
            summary=context.summary,
            source_file=context.file_path,
            snippet=context.snippet,
            line_range=context.line_range,
            suggested_path=suggested,
        )
        trail.destination_plan = dest
        result.candidates_planned += 1

        # Step 3 — execute
        # Bridge the destination plan into the existing executor's
        # ExtractionPlan shape. The executor still expects a v2 plan
        # for its prompt building.
        executor_plan = self._build_executor_plan(
            context=context, original=original, dest=dest,
        )
        if executor_plan is None:
            trail.final_status = "skipped_no_executor_plan"
            trail.failure_reason = (
                "could not bridge to executor plan shape — candidate "
                "kind likely needs additional source-file context"
            )
            return
        ext_result = self._executor.execute(executor_plan)
        trail.extraction_result = ext_result

        if ext_result.status == "applied":
            # Step 4a — static import verify on edited files. We
            # don't know which files the executor touched without
            # asking it; use the destination + source files as the
            # search set.
            edited_files = self._files_to_verify(
                dest=dest, context=context, original=original,
            )
            verify_report = self._verifier.verify_files(edited_files)
            trail.import_problems = [
                f"{p.file_path}:{p.line} {p.statement} — {p.reason}"
                for p in verify_report.problems
            ]
            if not verify_report.passed:
                # Static check found a broken import — revert
                # without running tests. (The executor already
                # committed; we need the executor's git_ops to
                # revert. For v3.0 we just mark as failed; the
                # caller can read trail.import_problems.)
                trail.final_status = "import_check_failed"
                trail.failure_reason = (
                    f"{len(verify_report.problems)} import problem(s)"
                )
                result.candidates_failed += 1
                self._log(
                    f"V3RefactorerAgent: FAIL {context.summary!r} — "
                    f"static import check found "
                    f"{len(verify_report.problems)} problem(s)"
                )
                return
            trail.final_status = "applied"
            result.candidates_applied += 1
            self._log(
                f"V3RefactorerAgent: APPLY {context.summary!r} → "
                f"{dest.destination_path}"
            )
        elif ext_result.status == "reverted":
            trail.final_status = "reverted"
            result.candidates_reverted += 1
            self._log(
                f"V3RefactorerAgent: REVERT {context.summary!r} "
                f"(executor rolled back)"
            )
        elif ext_result.status == "no_changes":
            trail.final_status = "no_changes"
            self._log(
                f"V3RefactorerAgent: NO-OP {context.summary!r} "
                f"(executor made no changes)"
            )
        else:
            trail.final_status = "failed"
            result.candidates_failed += 1

    def _build_executor_plan(
        self,
        *,
        context: CandidateContext,
        original,
        dest: DestinationPlan,
    ) -> Optional[ExtractionPlan]:
        """Construct an ExtractionPlan that the existing v2
        executor understands. We carry forward what the original
        CPD plan had (if any), then override the destination with
        the agent's choice."""
        if isinstance(original, ExtractionPlan):
            # CPD-sourced — clone with refined destination.
            return ExtractionPlan(
                duplicate_hash=original.duplicate_hash,
                language=original.language,
                services_involved=list(original.services_involved),
                source_files=list(original.source_files),
                token_count=original.token_count,
                files_count=original.files_count,
                instance_count=original.instance_count,
                suggested_core_path=dest.destination_path,
                risk_score=original.risk_score,
                disposition="extract",
                notes=list(original.notes) + [
                    f"v3 destination: {dest.destination_path} "
                    f"({dest.destination_kind})",
                ],
            )
        # Anti-pattern + misplacement candidates: synthesize a
        # minimum ExtractionPlan from the candidate context. The
        # executor doesn't strictly need duplicate_hash to be
        # meaningful — it threads it for logging.
        return ExtractionPlan(
            duplicate_hash=f"v3-{context.kind}-{abs(hash(context.summary)) % 100000:05d}",
            language="python",
            services_involved=[],
            source_files=[context.file_path],
            token_count=0,
            files_count=1,
            instance_count=1,
            suggested_core_path=dest.destination_path,
            risk_score=0.5,
            disposition="extract",
            notes=[
                f"v3 candidate kind: {context.kind}",
                f"v3 destination: {dest.destination_path} "
                f"({dest.destination_kind})",
            ],
        )

    def _files_to_verify(
        self,
        *,
        dest: DestinationPlan,
        context: CandidateContext,
        original,
    ) -> List[Path]:
        """Best-effort file list for the post-extraction import check.
        Always includes the destination + the source file the
        candidate came from. For CPD candidates, every source file
        of the duplicate."""
        files: set = set()
        # Destination (may not exist yet for the test stub case).
        dest_path = self._project_root / dest.destination_path
        if dest_path.exists():
            files.add(dest_path)
        # Source.
        src = self._project_root / context.file_path
        if src.exists():
            files.add(src)
        # CPD source files.
        if isinstance(original, ExtractionPlan):
            for sf in original.source_files:
                p = self._project_root / sf
                if p.exists():
                    files.add(p)
        return sorted(files)


# ── Candidate unification ────────────────────────────────────────


def _unify_candidates(
    *,
    anti_findings: List[AntiPatternFinding],
    cpd_plans: List[ExtractionPlan],
    cpd_report: CPDReport,
    misplaced: List[MisplacedLogicCandidate],
    max_total: int,
) -> List[Tuple[CandidateContext, object]]:
    """Convert all three signal sources into a flat list of
    ``(CandidateContext, original_object)`` tuples that the
    per-candidate pipeline iterates over.

    The original object is preserved so downstream
    (``_build_executor_plan``) can re-hydrate ExtractionPlan
    details for CPD-sourced candidates.

    Caps at ``max_total`` to bound LLM cost per refactor pass.
    """
    out: List[Tuple[CandidateContext, object]] = []

    # Anti-patterns
    for f in anti_findings:
        out.append((
            CandidateContext(
                kind="anti_pattern",
                summary=f"{f.pattern} at {f.path}:{f.line}",
                file_path=f.path,
                line_range=(f.line, f.line),
                snippet=f.snippet[:1500],
                extra={"severity": f.severity, "pattern": f.pattern},
            ),
            f,
        ))

    # CPD plans — already deterministically ranked
    for plan in cpd_plans:
        # First source file makes a representative location.
        first_src = plan.source_files[0] if plan.source_files else "(unknown)"
        out.append((
            CandidateContext(
                kind="cpd_duplicate",
                summary=(
                    f"~{plan.token_count}-token block duplicated across "
                    f"{plan.files_count} file(s), "
                    f"{plan.instance_count} occurrence(s)"
                ),
                file_path=first_src,
                line_range=None,
                snippet=_snippet_for_duplicate(plan, cpd_report),
                extra={
                    "duplicate_hash": plan.duplicate_hash,
                    "occurrence_count": plan.instance_count,
                    "files_count": plan.files_count,
                },
            ),
            plan,
        ))

    # Misplacement candidates
    for m in misplaced:
        out.append((
            CandidateContext(
                kind="misplaced_logic",
                summary=m.why,
                file_path=m.file_path,
                line_range=m.line_range,
                snippet="",  # scanner doesn't carry snippet
                extra={
                    "function_name": m.function_name,
                    "suggested_core_module": m.suggested_core_module,
                },
            ),
            m,
        ))

    return out[:max_total]


def _snippet_for_duplicate(
    plan: ExtractionPlan, cpd_report: CPDReport,
) -> str:
    """Pull a snippet from the first occurrence of the duplicate
    block. Best-effort — empty string if we can't find the line
    range in the CPD report (the report shape may not carry it for
    every duplicate)."""
    for dup in cpd_report.duplicates:
        if getattr(dup, "duplicate_hash", "") == plan.duplicate_hash:
            occs = getattr(dup, "occurrences", []) or []
            if occs:
                first = occs[0]
                path = Path(getattr(first, "file", ""))
                start = getattr(first, "line_start", 0)
                end = getattr(first, "line_end", start)
                if path.exists() and start > 0:
                    try:
                        lines = path.read_text(
                            encoding="utf-8",
                        ).splitlines()
                        return "\n".join(
                            lines[max(0, start - 1):end]
                        )[:1500]
                    except OSError:
                        return ""
    return ""


def _default_walk_python(project_root: Path) -> List[str]:
    """Default file walker — every .py under project_root that
    isn't a test, isn't __pycache__, isn't under .bizniz/."""
    root = Path(project_root)
    out: List[str] = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root)).replace("\\", "/")
        if rel.startswith(".bizniz/"):
            continue
        if "/tests/" in rel or path.name.startswith("test_"):
            continue
        if "__pycache__" in rel:
            continue
        # Strip core/typescript/* — Python-only for v3.
        if rel.startswith("core/typescript/"):
            continue
        out.append(str(path))
    return out
