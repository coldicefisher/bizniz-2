"""``ReviewRepairV5Loop`` — v5 monotone-convergence review/repair.

Per the v5 spec (docs/architecture/v5_canonical_findings_spec.md):

  Iter 1:
    Full parallel QE+CR review (today's v3.1 path)
    → freeze findings as CanonicalReport (persisted)
    → snapshot workspace via ProjectGit
  Iter 2+:
    Dispatch repair targeting CanonicalReport.unresolved()
    Snapshot before repair
    ResolutionChecker (per-source, parallel) → ResolutionReport
    Apply resolution → CanonicalReport mutates statuses
    If all blockers resolved → APPROVED
    If regression detected → rollback via ProjectGit, retry
    If no progress for N iters → stall halt

Returns the same outer tuple ``(coverage, code_review, result,
iteration_count, history_str)`` MilestoneLoop already consumes —
synthesizes legacy reports from CanonicalReport so downstream code
(run report, perf_log) keeps working.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.canonical_findings.persistence import (
    load_canonical_report, save_canonical_report,
)
from bizniz.canonical_findings.types import (
    CanonicalFinding, CanonicalReport, ResolutionDelta,
)
from bizniz.code_reviewer.types import CodeReviewReport, FlaggedSymbol
from bizniz.engineer.types import EngineerResult
from bizniz.quality_engineer.types import CoverageReport, MissingScenario
from bizniz.resolution_checker.adapters import (
    cr_report_to_canonical_findings, qe_coverage_to_canonical_findings,
)
from bizniz.resolution_checker.checker import (
    ResolutionChecker, check_both_sources_parallel,
)


class ReviewRepairV5Loop:
    """v5 review/repair loop. Same outer contract as v3.1's loop."""

    def __init__(
        self,
        *,
        # Closures from MilestoneLoop — let us reuse its existing
        # parallel-review machinery without copy-pasting.
        phase_review_parallel: Callable[..., tuple],  # returns (coverage, code_review)
        repair_dispatcher,  # has .repair(...)
        # Per-source resolution checkers — wired by v2_build.
        qe_resolution_checker: ResolutionChecker,
        cr_resolution_checker: ResolutionChecker,
        # For snapshot + rollback.
        project_git=None,  # Optional[ProjectGit]
        # Persistence path for the canonical report (resume-safe).
        canonical_path: Optional[Path] = None,
        # Function returning {path: content} for the workspace files
        # the resolution checker should examine. v2_build wires this
        # to read from the live workspace.
        snapshot_workspace_files: Optional[Callable[[List[str]], Dict[str, str]]] = None,
        # Optional: walks the live workspace and returns relative
        # paths of code+test files relevant to this milestone.
        # 2026-05-20 fix: prior versions only passed file_hint-tagged
        # paths to the checker, which left the checker with ZERO
        # files for QE coverage findings (no file_hint), so it
        # always voted still_present. v2_build wires this to a
        # workspace walker.
        discover_workspace_files: Optional[Callable[[], List[str]]] = None,
        # Optional escalation: PerMilestoneDebugger fires when the
        # loop is about to stall (stall_counter +1 with no progress).
        # If the debugger produces meaningful changes, the loop
        # re-checks before incrementing the stall counter further.
        milestone_debugger=None,  # Optional[PerMilestoneDebugger]
        stall_threshold: int = 3,
        hard_cap: int = 20,
        on_status: Optional[Callable[[str], None]] = None,
        # QE hybrid: after iter-1 review, call this closure to write
        # inline test patches for missing scenarios and validate them.
        # Returns the set of capability_ids that were auto-resolved by
        # a passing patch (those findings are excluded from the frozen
        # CanonicalReport). None = disabled (legacy behavior).
        # Signature: (coverage, enriched_spec) -> frozenset[str]
        qe_patch_and_apply: Optional[Callable] = None,
    ):
        self._phase_review_parallel = phase_review_parallel
        self._repair_dispatcher = repair_dispatcher
        self._qe_checker = qe_resolution_checker
        self._cr_checker = cr_resolution_checker
        self._project_git = project_git
        self._canonical_path = canonical_path
        self._snapshot_workspace_files = snapshot_workspace_files
        self._discover_workspace_files = discover_workspace_files
        self._milestone_debugger = milestone_debugger
        self._stall_threshold = max(1, int(stall_threshold))
        self._hard_cap = max(1, int(hard_cap))
        self._on_status = on_status
        self._qe_patch_and_apply = qe_patch_and_apply

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def run(
        self,
        *,
        milestone,
        architecture,
        spec,
        initial_result: EngineerResult,
        auth_contract: Optional[str],
        prior_list,
        milestone_index: int,
    ) -> tuple:
        """Execute the v5 loop. Returns
        ``(coverage, code_review, result, iter_count, history_str)``."""
        self._log("MilestoneLoop[v5]: entering review/repair loop")
        t0 = time.time()

        # ── Iter 1: full review ──────────────────────────────────
        coverage, code_review = self._phase_review_parallel(
            milestone, architecture, spec, initial_result,
            auth_contract, prior_list,
        )
        if coverage.approved and code_review.approved:
            self._log(
                "MilestoneLoop[v5]: approved on initial review (0 "
                "repair iter(s))"
            )
            return coverage, code_review, initial_result, 0, ""

        # QE hybrid: attempt inline test patches for missing scenarios.
        # Patches that validate clean auto-resolve their capability_ids
        # before we freeze — shrinks the CanonicalReport and may
        # eliminate entire repair iters for pure test-gap milestones.
        auto_resolved_cap_ids: frozenset = frozenset()
        if self._qe_patch_and_apply is not None:
            try:
                auto_resolved_cap_ids = frozenset(
                    self._qe_patch_and_apply(coverage, spec)
                )
                if auto_resolved_cap_ids:
                    self._log(
                        f"MilestoneLoop[v5]: QE auto-patched "
                        f"{len(auto_resolved_cap_ids)} capability id(s) — "
                        f"excluding from CanonicalReport: "
                        f"{sorted(auto_resolved_cap_ids)}"
                    )
            except Exception as e:
                self._log(
                    f"MilestoneLoop[v5]: QE patch failed "
                    f"({type(e).__name__}: {e}) — proceeding without patches"
                )

        # Freeze into canonical report.
        findings: List[CanonicalFinding] = []
        findings.extend(
            f for f in qe_coverage_to_canonical_findings(coverage)
            if f.capability_id not in auto_resolved_cap_ids
        )
        findings.extend(cr_report_to_canonical_findings(code_review))
        canonical = CanonicalReport(
            milestone_name=milestone.name,
            iter_frozen=1,
            findings=findings,
        )
        if self._canonical_path:
            try:
                save_canonical_report(canonical, self._canonical_path)
            except Exception as e:
                self._log(
                    f"MilestoneLoop[v5]: canonical persist failed "
                    f"(non-fatal): {type(e).__name__}: {e}"
                )
        self._log(
            f"MilestoneLoop[v5]: froze canonical report with "
            f"{len(canonical.findings)} finding(s) "
            f"({len(canonical.blockers())} blocker(s))"
        )

        result = initial_result
        stall_counter = 0
        history_lines: List[str] = [
            f"iter 1: {canonical.summary_line()}",
        ]
        repair_iter = 0

        # ── Iter 2+: repair → resolution check → apply ──────────
        while repair_iter < self._hard_cap:
            repair_iter += 1

            # Snapshot before repair (for rollback on regression).
            snapshot_tag: Optional[str] = None
            if self._project_git is not None:
                snapshot_tag = self._project_git.snapshot_for_repair_iter(
                    milestone_index=milestone_index,
                    iter_idx=repair_iter,
                )

            # Dispatch repair targeting canonical's unresolved findings.
            self._log(
                f"MilestoneLoop[v5]: repair iter {repair_iter} "
                f"(stall_counter={stall_counter}/{self._stall_threshold})"
            )
            try:
                result = self._repair_dispatcher.repair(
                    architecture=architecture,
                    enriched_spec=spec,
                    coverage_report=coverage,
                    code_review_report=code_review,
                    repair_iteration=repair_iter,
                    auth_contract=auth_contract,
                )
            except Exception as e:
                err_str = str(e)
                if "no-op" in err_str or "Refusing to ship" in err_str:
                    # Agent examined the code and concluded findings are
                    # already satisfied — no edits needed. Fall through
                    # to resolution check so the checker can confirm.
                    self._log(
                        f"MilestoneLoop[v5]: repair iter {repair_iter} "
                        f"returned no-op ({type(e).__name__}) — "
                        f"running resolution check anyway"
                    )
                    result = None
                else:
                    self._log(
                        f"MilestoneLoop[v5]: repair iter {repair_iter} "
                        f"raised: {type(e).__name__}: {e} — halting"
                    )
                    break

            # Resolution check: examine the current code against
            # canonical findings. Constrained output; no new findings
            # invented.
            current_files = self._collect_files_for_check(canonical)
            try:
                resolution = check_both_sources_parallel(
                    qe_checker=self._qe_checker,
                    cr_checker=self._cr_checker,
                    canonical=canonical,
                    iter_idx=repair_iter,
                    current_files=current_files,
                    on_status=self._on_status,
                )
            except Exception as e:
                self._log(
                    f"MilestoneLoop[v5]: resolution check raised: "
                    f"{type(e).__name__}: {e} — halting"
                )
                break

            delta = canonical.apply_resolution(resolution, iter_idx=repair_iter)
            history_lines.append(
                f"iter {repair_iter}: delta resolved={delta.progress_count}, "
                f"regressed={delta.regression_count}; {canonical.summary_line()}"
            )

            # Persist canonical after each iter (resume-safe).
            if self._canonical_path:
                try:
                    save_canonical_report(canonical, self._canonical_path)
                except Exception:
                    pass

            # Regression: roll back this iter's repair changes.
            if delta.is_regression and snapshot_tag and self._project_git is not None:
                self._log(
                    f"MilestoneLoop[v5]: REGRESSION at iter {repair_iter} "
                    f"({delta.regression_count} regressed) — rolling back "
                    f"to {snapshot_tag}"
                )
                self._project_git.rollback_repair_iter(
                    milestone_index=milestone_index,
                    iter_idx=repair_iter,
                )
                # Reset the affected findings back to their pre-repair status.
                # ``apply_resolution`` flipped some from resolved to regressed;
                # the rollback undid the code change so restore to resolved.
                for fid in delta.regressed_ids:
                    f = canonical.by_id().get(fid)
                    if f and f.status == "regressed":
                        f.status = "resolved"
                stall_counter += 1
                if stall_counter >= self._stall_threshold:
                    self._log(
                        f"MilestoneLoop[v5]: stall threshold reached after "
                        f"{repair_iter} iter(s) — halting"
                    )
                    break
                continue

            # Approval check.
            if canonical.all_blockers_resolved():
                self._log(
                    f"MilestoneLoop[v5]: APPROVED after {repair_iter} "
                    f"repair iter(s) — all blockers resolved"
                )
                # Build a synthetic "approved" coverage + code_review pair.
                coverage_synth, code_review_synth = self._synthesize_reports(
                    milestone=milestone, canonical=canonical, approved=True,
                )
                wall = time.time() - t0
                return (
                    coverage_synth, code_review_synth, result,
                    repair_iter, "\n".join(history_lines + [f"wall={wall:.1f}s"]),
                )

            # Progress check.
            if delta.progress_count == 0:
                # Before incrementing stall counter, try the milestone
                # debugger (if wired). It sees the whole milestone and
                # has full Bash/Edit access — may resolve what the
                # structured loop couldn't.
                if self._milestone_debugger is not None:
                    self._log(
                        f"MilestoneLoop[v5]: no progress at iter "
                        f"{repair_iter} — escalating to PerMilestoneDebugger"
                    )
                    try:
                        debug_result = self._milestone_debugger.debug(
                            milestone_name=canonical.milestone_name,
                            findings=list(canonical.unresolved()),
                            current_files=self._collect_files_for_check(canonical),
                        )
                    except Exception as e:
                        self._log(
                            f"MilestoneLoop[v5]: milestone debugger raised: "
                            f"{type(e).__name__}: {e}"
                        )
                        debug_result = None

                    if debug_result and debug_result.files_touched:
                        # Re-run the resolution check against the
                        # post-debug workspace. If progress, reset stall.
                        try:
                            post_resolution = check_both_sources_parallel(
                                qe_checker=self._qe_checker,
                                cr_checker=self._cr_checker,
                                canonical=canonical,
                                iter_idx=repair_iter,
                                current_files=self._collect_files_for_check(canonical),
                                on_status=self._on_status,
                            )
                            post_delta = canonical.apply_resolution(
                                post_resolution, iter_idx=repair_iter,
                            )
                            history_lines.append(
                                f"iter {repair_iter} [post-debug]: delta "
                                f"resolved={post_delta.progress_count}; "
                                f"{canonical.summary_line()}"
                            )
                            if post_delta.progress_count > 0:
                                # Debugger rescued progress — don't stall.
                                stall_counter = 0
                                if canonical.all_blockers_resolved():
                                    self._log(
                                        f"MilestoneLoop[v5]: APPROVED after "
                                        f"debugger rescue at iter {repair_iter}"
                                    )
                                    coverage_synth, code_review_synth = (
                                        self._synthesize_reports(
                                            milestone=milestone,
                                            canonical=canonical, approved=True,
                                        )
                                    )
                                    wall = time.time() - t0
                                    return (
                                        coverage_synth, code_review_synth,
                                        result, repair_iter,
                                        "\n".join(history_lines + [f"wall={wall:.1f}s"]),
                                    )
                                # Progress made but not approved — next iter.
                                continue
                        except Exception as e:
                            self._log(
                                f"MilestoneLoop[v5]: post-debug resolution "
                                f"check raised: {type(e).__name__}: {e}"
                            )

                stall_counter += 1
                if stall_counter >= self._stall_threshold:
                    self._log(
                        f"MilestoneLoop[v5]: stall threshold reached after "
                        f"{repair_iter} iter(s) ({len(canonical.blockers())} "
                        f"blocker(s) remain) — halting"
                    )
                    break
            else:
                stall_counter = 0

        # Loop exhausted (hard cap or stall halt).
        wall = time.time() - t0
        coverage_synth, code_review_synth = self._synthesize_reports(
            milestone=milestone, canonical=canonical, approved=False,
        )
        return (
            coverage_synth, code_review_synth, result,
            repair_iter, "\n".join(history_lines + [f"wall={wall:.1f}s"]),
        )

    # ── Helpers ──────────────────────────────────────────────────

    def _collect_files_for_check(
        self, canonical: CanonicalReport,
    ) -> Dict[str, str]:
        """Collect workspace files for the ResolutionChecker to inspect.

        v14 anchor (2026-05-20): the prior version only passed paths
        referenced by ``file_hint``. Most QE coverage findings
        reference a ``capability_id`` (no file_hint), so the
        checker was given ZERO code and judged every finding as
        ``still_present`` forever. Loop never converged.

        Fix: also call ``discover_workspace_files()`` to get ALL
        code + test files in the milestone's workspace. The
        checker has the real material to inspect.

        Cap total file count to keep the prompt bounded; per-file
        char cap lives in the checker's prompt builder.
        """
        if self._snapshot_workspace_files is None:
            return {}

        # Start with file_hint paths (CR critical findings).
        paths: set = {
            f.file_hint for f in canonical.findings
            if f.file_hint and f.status not in ("resolved", "wont_fix")
        }

        # Augment with all relevant workspace files via the
        # discover closure (if wired). Pre-2026-05-20 callers may
        # not pass one — fall back gracefully.
        if self._discover_workspace_files is not None:
            try:
                discovered = self._discover_workspace_files()
                paths.update(discovered)
            except Exception:
                pass

        # Priority-sort before capping: file_hint paths first (most
        # relevant to specific CR findings), then test files (QE
        # coverage findings need to see tests), then everything else.
        # Alphabetical within each tier for determinism.
        file_hints = {
            f.file_hint for f in canonical.findings
            if f.file_hint and f.status not in ("resolved", "wont_fix")
        }
        test_files = {
            p for p in paths
            if "/test_" in p or p.startswith("test_") or "/tests/" in p
        }
        other_files = paths - file_hints - test_files
        ordered = (
            sorted(file_hints)
            + sorted(test_files - file_hints)
            + sorted(other_files)
        )
        capped = ordered[:60]
        if not capped:
            self._log(
                "ReviewRepairV5: _collect_files_for_check returned 0 "
                "paths (no file_hints AND discover_workspace_files "
                "yielded nothing) — checker will fly blind"
            )
            return {}
        try:
            files = self._snapshot_workspace_files(capped)
        except Exception as e:
            self._log(
                f"ReviewRepairV5: snapshot_workspace_files raised "
                f"{type(e).__name__}: {e} — checker will fly blind"
            )
            return {}
        sample = sorted(files.keys())[:5]
        self._log(
            f"ReviewRepairV5: _collect_files_for_check assembled "
            f"{len(files)} file(s) for checker (asked for "
            f"{len(capped)}); sample={sample}"
        )
        return files

    def _synthesize_reports(
        self, *,
        milestone, canonical: CanonicalReport, approved: bool,
    ) -> tuple:
        """Build legacy CoverageReport + CodeReviewReport from the
        canonical report so downstream consumers (run report,
        perf_log) keep working."""
        qe_findings = [f for f in canonical.findings if f.source == "quality_engineer" and not f.status == "resolved"]
        cr_findings = [f for f in canonical.findings if f.source == "code_reviewer" and not f.status == "resolved"]

        coverage = CoverageReport(
            milestone_name=milestone.name,
            approved=approved,
            missing_scenarios=[
                MissingScenario(
                    capability_id=f.capability_id or "unknown",
                    scenario=f.detail or f.summary,
                    priority=("critical" if f.priority == "critical" else "important"),
                )
                for f in qe_findings
            ],
            summary=f"v5 canonical: {canonical.summary_line()}",
        )
        code_review = CodeReviewReport(
            milestone_name=milestone.name,
            approved=approved or not any(
                f.priority in ("critical", "important") for f in cr_findings
            ),
            flagged_symbols=[
                FlaggedSymbol(
                    file=f.file_hint or "unknown",
                    symbol=f.summary[:50],
                    kind="import",
                    reason=f.detail[:200] if f.detail else f.summary,
                    severity="critical",
                )
                for f in cr_findings if f.priority == "critical"
            ],
            summary=f"v5 canonical: {canonical.summary_line()}",
        )
        return coverage, code_review
