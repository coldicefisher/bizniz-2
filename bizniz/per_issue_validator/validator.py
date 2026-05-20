"""``PerIssueValidator`` — write + scan + fix-loop for one issue.

Workflow per issue:

  1. Write the CoderTesterResult's filled_files to disk
  2. Run deterministic scanners (symbol_validator + AST via syntax_errors)
  3. Optionally run ``pytest --collect-only`` on the test files
  4. If clean → return ValidatedIssue(clean=True)
  5. Else loop:
       - Build a fix-pass prompt that includes findings + current file content
       - Re-invoke CoderTesterAgent
       - Write new files
       - Re-scan
       - Stall check: if findings don't decrease, bail
  6. Return ValidatedIssue with final state (clean or not)

The agent invocations in the fix loop are "agentic debug" — the
same LLM reasoning over the same context, now seeing what the
scanners found. No tool-loop overhead; just structured output.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from bizniz.architect.types import ServiceDefinition
from bizniz.coder.symbol_validator import validate_files
from bizniz.coder.types import Issue
from bizniz.coder_tester.agent import CoderTesterAgent, CoderTesterError
from bizniz.coder_tester.types import CoderTesterResult, FilledFile
from bizniz.per_issue_validator.types import Finding, ValidatedIssue
from bizniz.quality_engineer.types import CapabilitySpec
from bizniz.workspace.base_workspace import BaseWorkspace


class PerIssueValidator:
    """Per-issue write + scan + fix-loop runner."""

    def __init__(
        self,
        *,
        agent: CoderTesterAgent,
        workspace: BaseWorkspace,
        on_status: Optional[Callable[[str], None]] = None,
        run_pytest_collect: bool = True,
        stall_threshold: int = 3,
        hard_cap: int = 10,
    ):
        self._agent = agent
        self._workspace = workspace
        self._on_status = on_status
        self._run_pytest_collect = run_pytest_collect
        self._stall_threshold = stall_threshold
        self._hard_cap = hard_cap

    def validate(
        self,
        *,
        issue: Issue,
        initial_result: CoderTesterResult,
        service: ServiceDefinition,
        capabilities: List[CapabilitySpec],
        seeded_files: List[FilledFile],
        skeleton_md: Optional[str] = None,
        auth_contract: Optional[str] = None,
        sibling_issue_summaries: Optional[List[str]] = None,
    ) -> ValidatedIssue:
        """Run write + scan + fix-loop on one issue."""
        self._log(
            f"PerIssueValidator[{issue.id}]: starting "
            f"({len(initial_result.filled_files)} file(s) to write)"
        )

        # Step 1: Write initial result to disk.
        files_written = self._write_files(initial_result.filled_files)

        # Step 2: Scan.
        findings = self._scan(issue, files_written)
        prior_count = len(findings)

        if not findings:
            self._log(
                f"PerIssueValidator[{issue.id}]: clean on first pass "
                f"({len(files_written)} file(s) written)"
            )
            return ValidatedIssue(
                issue_id=issue.id,
                clean=True,
                files_written=files_written,
                findings=[],
                debug_iterations=0,
            )

        # Step 3: Fix-loop.
        debug_iter = 0
        stall_counter = 0
        last_result = initial_result
        while debug_iter < self._hard_cap:
            debug_iter += 1
            self._log(
                f"PerIssueValidator[{issue.id}]: debug iter {debug_iter}, "
                f"{prior_count} finding(s)"
            )

            try:
                fix_result = self._invoke_fix_pass(
                    issue=issue,
                    service=service,
                    capabilities=capabilities,
                    seeded_files=seeded_files,
                    findings=findings,
                    prior_result=last_result,
                    skeleton_md=skeleton_md,
                    auth_contract=auth_contract,
                    sibling_issue_summaries=sibling_issue_summaries,
                )
            except CoderTesterError as e:
                self._log(
                    f"PerIssueValidator[{issue.id}]: agent error on debug "
                    f"iter {debug_iter} — {type(e).__name__}: {e}"
                )
                return ValidatedIssue(
                    issue_id=issue.id,
                    clean=False,
                    files_written=files_written,
                    findings=findings,
                    debug_iterations=debug_iter,
                    halt_reason=f"agent_error: {type(e).__name__}: {e}",
                )

            new_files = self._write_files(fix_result.filled_files)
            for p in new_files:
                if p not in files_written:
                    files_written.append(p)
            last_result = fix_result

            findings = self._scan(issue, files_written)
            cur_count = len(findings)

            if cur_count == 0:
                self._log(
                    f"PerIssueValidator[{issue.id}]: clean after "
                    f"{debug_iter} debug iter(s)"
                )
                return ValidatedIssue(
                    issue_id=issue.id,
                    clean=True,
                    files_written=files_written,
                    findings=[],
                    debug_iterations=debug_iter,
                )

            # Progress check — only progress resets the stall counter.
            if cur_count < prior_count:
                stall_counter = 0
                self._log(
                    f"PerIssueValidator[{issue.id}]: progress "
                    f"({prior_count} → {cur_count})"
                )
            else:
                stall_counter += 1
                self._log(
                    f"PerIssueValidator[{issue.id}]: no progress "
                    f"({prior_count} → {cur_count}); stall {stall_counter}"
                    f"/{self._stall_threshold}"
                )
                if stall_counter >= self._stall_threshold:
                    self._log(
                        f"PerIssueValidator[{issue.id}]: stall threshold "
                        f"reached — halting with {cur_count} finding(s)"
                    )
                    return ValidatedIssue(
                        issue_id=issue.id,
                        clean=False,
                        files_written=files_written,
                        findings=findings,
                        debug_iterations=debug_iter,
                        halt_reason="stall",
                    )

            prior_count = cur_count

        # Hard cap.
        self._log(
            f"PerIssueValidator[{issue.id}]: hard cap ({self._hard_cap}) "
            f"reached — halting with {len(findings)} finding(s)"
        )
        return ValidatedIssue(
            issue_id=issue.id,
            clean=False,
            files_written=files_written,
            findings=findings,
            debug_iterations=debug_iter,
            halt_reason="hard_cap",
        )

    # ── Helpers ────────────────────────────────────────────────────

    def _write_files(self, filled: List[FilledFile]) -> List[str]:
        written: List[str] = []
        for f in filled:
            self._workspace.write_file(f.path, f.content)
            written.append(f.path)
        return written

    def _scan(self, issue: Issue, files_written: List[str]) -> List[Finding]:
        """Run all deterministic scanners; return one Finding list.

        Only BLOCKING findings appear in the returned list — the
        per-issue fix-loop iterates on these. ``unresolved_attribute``
        findings (from symbol_validator's attribute-access check) are
        treated as ADVISORY: they're noisy on framework-magic patterns
        (Pydantic ``model_fields``, SQLAlchemy ``__tablename__``,
        SQLAlchemy ``Base.registry``) and the v4 live run on
        recipe_v4_v4 (2026-05-19) showed the agent ping-ponging
        between equally-valid alternatives to satisfy false positives.
        Genuine attribute hallucinations still surface downstream in
        QE + CR review. Logged here so the operator can spot them
        without blocking the loop.
        """
        findings: List[Finding] = []
        advisory_count = 0

        # Python symbol + AST validation. Skip for non-python issues
        # (TypeScript validation is deferred per symbol_validator.py).
        if (issue.language or "python").lower() == "python":
            workspace_root = self._workspace.path(".")
            py_paths = [
                self._workspace.path(p) for p in files_written
                if p.endswith(".py")
            ]
            if py_paths:
                report = validate_files(py_paths, workspace_root)
                for syn in report.syntax_errors:
                    findings.append(Finding(
                        source="ast", message=syn, raw=syn,
                    ))
                for u in report.unresolved:
                    findings.append(Finding(
                        source="symbol_validator",
                        file=u.file,
                        line=u.line,
                        message=f"unresolved {u.kind}: {u.symbol} ({u.reason})",
                        raw=f"{u.file}:{u.line} {u.symbol} — {u.reason}",
                    ))
                # unresolved_attributes are ADVISORY — log count, don't
                # surface as blocking findings (see docstring).
                advisory_count = len(report.unresolved_attributes)

        if self._run_pytest_collect:
            findings.extend(self._pytest_collect(issue, files_written))

        if advisory_count:
            self._log(
                f"PerIssueValidator[{issue.id}]: {advisory_count} "
                f"attribute-access advisor(y/ies) — not blocking (logged)"
            )

        return findings

    def _pytest_collect(
        self, issue: Issue, files_written: List[str],
    ) -> List[Finding]:
        """Run ``pytest --collect-only`` on the issue's test files to
        catch import-level / fixture-level brokenness that
        symbol_validator can't detect (e.g. missing fixtures, decorator
        usage errors). Runs on host Python — same env the workspace
        already uses for the rest of the pipeline.
        """
        if (issue.language or "python").lower() != "python":
            return []
        test_paths = [
            self._workspace.path(p) for p in files_written
            if p in issue.test_files and p.endswith(".py")
        ]
        if not test_paths:
            return []
        workspace_root = self._workspace.path(".")
        try:
            proc = subprocess.run(
                [
                    "python", "-m", "pytest",
                    "--collect-only", "-q",
                    *[str(p) for p in test_paths],
                ],
                cwd=str(workspace_root),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return [Finding(
                source="pytest_collect",
                message="pytest --collect-only timed out (>60s)",
            )]
        except FileNotFoundError:
            # No pytest on the host PATH — skip silently.
            return []
        if proc.returncode == 0:
            return []
        tail = (proc.stdout or "") + "\n" + (proc.stderr or "")
        # Trim aggressive — keep last 2000 chars for context.
        tail = tail[-2000:]
        return [Finding(
            source="pytest_collect",
            message=f"pytest --collect-only failed (exit {proc.returncode})",
            raw=tail,
        )]

    def _invoke_fix_pass(
        self,
        *,
        issue: Issue,
        service: ServiceDefinition,
        capabilities: List[CapabilitySpec],
        seeded_files: List[FilledFile],
        findings: List[Finding],
        prior_result: CoderTesterResult,
        skeleton_md: Optional[str],
        auth_contract: Optional[str],
        sibling_issue_summaries: Optional[List[str]],
    ) -> CoderTesterResult:
        """Re-invoke CoderTesterAgent with findings as context.

        We pass the agent the current (broken) version of the files as
        the seeded scaffold so it can see what it just wrote, plus a
        synthetic capability appended to the spec list that summarizes
        the findings. The agent re-emits the same paths with fixes.
        """
        # Build a "findings" capability the agent sees in its prompt.
        findings_summary = _render_findings_for_prompt(findings)
        fix_cap = CapabilitySpec(
            id="_fix_findings",
            name="Fix validator findings (do not skip)",
            description=(
                "The previous pass produced files that failed validation. "
                "Fix every finding below WITHOUT changing the contract "
                "(signatures, imports the rest of the milestone depends on). "
                "Re-emit the same files with corrections.\n\n"
                + findings_summary
            ),
        )

        # Use the current on-disk content as the new "seeded" scaffold
        # so the agent sees its prior attempt.
        current_seed: List[FilledFile] = []
        for f in prior_result.filled_files:
            current_seed.append(FilledFile(
                path=f.path, content=f.content, role=f.role,
            ))
        # Augment with the original seeded scaffold (might overlap; agent
        # tolerates dupes — uses the most-recent path entry).
        seen = {f.path for f in current_seed}
        for s in seeded_files:
            if s.path not in seen:
                current_seed.append(s)

        augmented_caps = list(capabilities) + [fix_cap]
        # Issue spec_refs may not include `_fix_findings`; the prompt's
        # capability section also filters by spec_refs, so we patch the
        # issue's spec_refs to include the synthetic one for THIS call.
        patched_issue = issue.model_copy(update={
            "spec_refs": list(issue.spec_refs or []) + ["_fix_findings"],
        })

        return self._agent.code_issue(
            issue=patched_issue,
            service=service,
            seeded_files=current_seed,
            capabilities=augmented_caps,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
            sibling_issue_summaries=sibling_issue_summaries,
        )

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass


# ── Helpers ────────────────────────────────────────────────────────


def _render_findings_for_prompt(findings: List[Finding]) -> str:
    """Render findings as a compact bullet list for the agent."""
    if not findings:
        return ""
    by_source: dict = {}
    for f in findings:
        by_source.setdefault(f.source, []).append(f)
    parts: List[str] = []
    for src, items in by_source.items():
        parts.append(f"**{src}** ({len(items)} finding(s)):")
        for f in items[:20]:  # cap to keep prompt sane
            loc = ""
            if f.file:
                loc = f" ({f.file}" + (f":{f.line}" if f.line else "") + ")"
            parts.append(f"  - {f.message}{loc}")
        if len(items) > 20:
            parts.append(f"  - ... and {len(items) - 20} more")
    return "\n".join(parts)
