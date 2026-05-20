"""``PerIssueDebugger`` — full tool-loop debugger per issue (v4 Option 3).

Modeled after ``ClaudeCliDebugger`` but scoped to ONE issue.
Replaces (or supplements) the structured-output fix-loop in
``PerIssueValidator`` with a Claude CLI tool-loop session that has
Edit/Write/Read/Bash/Glob/Grep access against the service workspace.

The user's mental model (2026-05-19):
  - Coders frame quickly (CoderTesterAgent, parallel).
  - Debugger gets full capability (tool-loop, sequential).
  - Context truncated when too long.

This module is the "debugger" half. Wires into PerIssueValidator
as the fallback when the structured fix-loop can't converge — or as
the primary fix-path when explicitly preferred.

Why a separate class vs. reusing ClaudeCliDebugger directly:
  - ClaudeCliDebugger is integration-stack scoped (whole compose
    stack, full pytest output). PerIssueDebugger is one-issue scoped
    (one file set, one capability spec).
  - Different prompt structure (issue + findings, not test output).
  - Returns ValidatedIssue (PerIssueValidator's contract) instead of
    AgenticDiagnosis.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from bizniz.architect.types import ServiceDefinition
from bizniz.coder.types import Issue
from bizniz.coder_tester.types import FilledFile
from bizniz.per_issue_validator.types import Finding, ValidatedIssue
from bizniz.quality_engineer.types import CapabilitySpec
from bizniz.workspace.base_workspace import BaseWorkspace


_ALLOWED_TOOLS = ["Edit", "Write", "Read", "Bash", "Glob", "Grep"]


_DEBUGGER_SYSTEM_PROMPT = """You are a debugging agent fixing ONE
atomic issue's code + tests. A scaffold-stage coder already wrote
an initial pass; the deterministic validators surfaced findings you
need to drive to zero.

# Toolbox
- **Edit / Write / Read**: file ops on this issue's files. The
  workspace is your current directory.
- **Bash**: run the project's scanners + tests directly. Common
  moves:
    - ``python -c "from app.X import Y"`` to verify imports resolve
    - ``docker compose -f <compose> exec -T <svc> python -m pytest
      --collect-only tests/test_X.py`` to verify pytest can load
      the test file (deps live in the container)
    - ``docker compose -f <compose> exec -T <svc> python -m pytest
      tests/test_X.py -x -q`` to actually RUN the tests
- **Glob / Grep**: find sibling files, find symbol definitions in
  other issues' code.

# Your scope
You're working on ONE issue. Stay within:
  - ``target_files`` (the issue's code files)
  - ``test_files`` (the issue's test files)
Do NOT edit files outside this set. If you find a defect outside
your scope, note it in your final report but don't fix it here.

# Workflow
1. **Read the findings** the dispatcher hands you. They name the
   real problems (unresolved imports, syntax errors, pytest
   collection failures).
2. **Investigate** with Bash + Read. Verify the finding actually
   reproduces. Check if it's a real defect or a scanner false
   positive.
3. **Fix** with Edit / Write. Make the smallest change that closes
   the finding without breaking the issue's contract.
4. **Verify** with Bash: re-run the same scanner that flagged it.
   The finding should be gone.
5. **Iterate** until findings are zero OR you've determined the
   remaining findings are not real defects (e.g., framework-magic
   patterns the scanner can't reason about).

# When you're done
Stop using tools and emit a final line of free-text:
``DEBUGGER_DONE: status=<clean|partial>, files_touched=[a.py, b.py]``
The dispatcher parses that line to confirm completion.

# Constraints
- Tests must be REAL — real assertions, real fixtures, no
  ``assert True`` stubs.
- Imports must actually resolve in the project's environment.
- Code and tests must stay aligned (you may edit both).
- Do not silently delete a test that's failing — figure out why
  it's failing and fix the code.
"""


def _truncate(text: str, max_chars: int = 4000) -> str:
    """Truncate from the middle, keeping head + tail."""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2 - 50
    tail = max_chars - head - 100
    return (
        text[:head]
        + f"\n\n...[truncated {len(text) - head - tail} chars]...\n\n"
        + text[-tail:]
    )


def _truncate_findings(findings: List[Finding], cap: int = 20) -> List[Finding]:
    """Most recent N findings only. (PerIssueValidator's _scan emits
    in deterministic order; we keep the head as 'real-looking' first.)"""
    return findings[:cap]


class PerIssueDebuggerError(Exception):
    """Subprocess returned non-zero or could not parse final status."""


class PerIssueDebugger:
    """Tool-loop debugger for ONE issue. Sequential by design.

    Use this when the structured CoderTesterAgent fix-loop can't
    converge (false-positive churn, deep dep issues that require
    running the actual tests in-container, etc.).
    """

    def __init__(
        self,
        *,
        workspace: BaseWorkspace,
        compose_path: Optional[str] = None,
        service_name: Optional[str] = None,
        # Bumped 600 → 3000 (2026-05-19 evening). recipe_v4_v8 saw
        # BA-fix1-1 timeout at 10 min mid-investigation, leaving
        # partial work that AST-passed but was semantically incomplete
        # — contributed to the iter-3 regression. Real debugging cases
        # (deep dep wiring, integration test repair) routinely need
        # 30+ min of tool-loop iteration. 50 min ceiling is generous
        # but bounded; the agent decides when it's done via
        # DEBUGGER_DONE line, this is just the safety net.
        timeout_seconds: int = 3000,
        on_status: Optional[Callable[[str], None]] = None,
        command: str = "claude",
        additional_args: Optional[List[str]] = None,
        # Truncation knobs — keep prompt bounded as context grows.
        max_file_chars: int = 6000,
        max_findings: int = 20,
    ):
        self._workspace = workspace
        self._compose_path = compose_path or ""
        self._service_name = service_name or ""
        self._timeout_s = float(timeout_seconds)
        self._on_status = on_status
        self._command = command
        self._additional_args = list(additional_args or [])
        self._max_file_chars = max_file_chars
        self._max_findings = max_findings

        if shutil.which(self._command) is None:
            raise PerIssueDebuggerError(
                f"PerIssueDebugger: ``{self._command}`` not on PATH."
            )

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def debug(
        self,
        *,
        issue: Issue,
        service: ServiceDefinition,
        current_files: List[FilledFile],
        findings: List[Finding],
        capabilities: List[CapabilitySpec],
        skeleton_md: Optional[str] = None,
        auth_contract: Optional[str] = None,
    ) -> ValidatedIssue:
        """Run the tool-loop debugger on one issue's findings.

        ``current_files`` is what's on disk now (the coder's last
        output). Claude reads from the workspace via Read — we pass
        these for prompt context only (so the agent doesn't have to
        Read each file separately if the content's short enough to
        inline).

        Returns ValidatedIssue with ``clean`` based on what the
        debugger reports in its final ``DEBUGGER_DONE`` line. The
        outer PerIssueValidator should run a fresh scan after this
        returns to confirm.
        """
        t0 = time.time()
        self._log(
            f"PerIssueDebugger[{issue.id}]: starting "
            f"({len(findings)} finding(s), "
            f"{len(current_files)} file(s) to read)"
        )

        prompt = self._build_prompt(
            issue=issue,
            service=service,
            current_files=current_files,
            findings=findings,
            capabilities=capabilities,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
        )

        ws_root = self._resolve_workspace_root()
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", _DEBUGGER_SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(_ALLOWED_TOOLS),
            "--add-dir", str(ws_root),
        ] + self._additional_args

        env = os.environ.copy()

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                cwd=str(ws_root),
                env=env,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            self._log(
                f"PerIssueDebugger[{issue.id}]: TIMEOUT after "
                f"{elapsed:.1f}s — partial work may remain on disk"
            )
            return ValidatedIssue(
                issue_id=issue.id,
                clean=False,
                files_written=[f.path for f in current_files],
                findings=findings,
                debug_iterations=1,
                halt_reason="debugger_timeout",
            )
        elapsed = time.time() - t0
        self._log(
            f"PerIssueDebugger[{issue.id}]: subprocess done in "
            f"{elapsed:.1f}s (exit {proc.returncode})"
        )

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout)[:400]
            return ValidatedIssue(
                issue_id=issue.id,
                clean=False,
                files_written=[f.path for f in current_files],
                findings=findings,
                debug_iterations=1,
                halt_reason=f"debugger_exit_{proc.returncode}: {tail}",
            )

        # Parse the final status line out of Claude's stdout.
        # Claude's claude --print emits a JSON payload — the actual
        # text is in payload["result"].
        import json
        try:
            payload = json.loads(proc.stdout)
            result_text = payload.get("result") or ""
        except json.JSONDecodeError:
            # Sometimes the CLI emits plain text; tolerate.
            result_text = proc.stdout

        status = "partial"
        files_touched: List[str] = []
        for line in result_text.splitlines():
            line = line.strip()
            if line.startswith("DEBUGGER_DONE:"):
                # e.g. "DEBUGGER_DONE: status=clean, files_touched=[a.py, b.py]"
                if "status=clean" in line:
                    status = "clean"
                elif "status=partial" in line:
                    status = "partial"
                # Best-effort parse files_touched.
                if "files_touched=[" in line:
                    head = line.split("files_touched=[", 1)[1]
                    inner = head.split("]", 1)[0]
                    files_touched = [
                        s.strip().strip("'\"")
                        for s in inner.split(",") if s.strip()
                    ]

        # Default the touched list to the input set if the agent
        # didn't echo it.
        if not files_touched:
            files_touched = [f.path for f in current_files]

        clean = status == "clean"
        self._log(
            f"PerIssueDebugger[{issue.id}]: status={status}, "
            f"files_touched={files_touched}"
        )
        return ValidatedIssue(
            issue_id=issue.id,
            clean=clean,
            files_written=files_touched,
            findings=[] if clean else findings,
            debug_iterations=1,
            halt_reason="" if clean else "debugger_partial",
        )

    # ── Prompt + context truncation ──────────────────────────────

    def _build_prompt(
        self,
        *,
        issue: Issue,
        service: ServiceDefinition,
        current_files: List[FilledFile],
        findings: List[Finding],
        capabilities: List[CapabilitySpec],
        skeleton_md: Optional[str],
        auth_contract: Optional[str],
    ) -> str:
        sections: List[str] = []
        sections.append(f"## Issue\n")
        sections.append(f"### `{issue.id}` — {issue.title}")
        sections.append(issue.description)
        sections.append(f"\n- target_files: {issue.target_files}")
        sections.append(f"- test_files: {issue.test_files}")
        if issue.success_criteria:
            sections.append("- success criteria:")
            for sc in issue.success_criteria:
                sections.append(f"  - {sc}")

        sections.append(f"\n## Service\n")
        sections.append(f"- name: `{service.name}`")
        sections.append(f"- framework: {service.framework} / {service.language}")
        if self._compose_path:
            sections.append(f"- compose: `{self._compose_path}`")
        if self._service_name:
            sections.append(f"- container svc: `{self._service_name}`")
            sections.append(
                f"- in-container pytest: "
                f"``docker compose -f {self._compose_path} exec -T "
                f"{self._service_name} python -m pytest tests/...``"
            )

        sections.append("\n## Findings to fix\n")
        truncated = _truncate_findings(findings, self._max_findings)
        for f in truncated:
            loc = ""
            if f.file:
                loc = f" ({f.file}" + (f":{f.line}" if f.line else "") + ")"
            sections.append(f"- **{f.source}**: {f.message}{loc}")
            if f.raw:
                raw_short = f.raw[:500]
                sections.append(f"  ```")
                sections.append(f"  {raw_short}")
                sections.append(f"  ```")
        if len(findings) > self._max_findings:
            sections.append(
                f"\n_({len(findings) - self._max_findings} more findings "
                f"truncated — focus on the ones above first)_"
            )

        relevant_caps = [c for c in capabilities if c.id in (issue.spec_refs or [])]
        if relevant_caps:
            sections.append("\n## Relevant capability spec\n")
            for c in relevant_caps:
                sections.append(f"### `{c.id}` — {c.name}")
                if c.description:
                    sections.append(c.description)
                if c.test_scenarios:
                    sections.append("**Test scenarios required:**")
                    for ts in c.test_scenarios:
                        sections.append(f"  - {ts}")

        sections.append("\n## Current file contents (on disk now)\n")
        for ff in current_files:
            sections.append(f"### `{ff.path}` (role: {ff.role})")
            sections.append("```")
            sections.append(_truncate(ff.content, self._max_file_chars))
            sections.append("```")

        if skeleton_md:
            sections.append(f"\n## Skeleton contract\n\n{_truncate(skeleton_md, 3000)}")
        if auth_contract:
            sections.append(f"\n## Auth contract\n\n{_truncate(auth_contract, 2000)}")

        sections.append(
            "\n## Your job\n\n"
            "Investigate the findings, fix them in-place using Edit/Write, "
            "and verify with Bash (re-run the scanner that flagged them). "
            "Stay within this issue's target_files + test_files. "
            "When done — even partially — emit a final line:\n\n"
            "  DEBUGGER_DONE: status=<clean|partial>, files_touched=[a.py, b.py]"
        )

        return "\n".join(sections)

    def _resolve_workspace_root(self) -> Path:
        ws_root = getattr(self._workspace, "root", None)
        if ws_root is None:
            return Path.cwd()
        return Path(str(ws_root))
