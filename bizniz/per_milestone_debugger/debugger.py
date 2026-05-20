"""PerMilestoneDebugger — milestone-scoped tool-loop debugger.

Structurally similar to PerIssueDebugger but wider scope:
  - Sees the whole project workspace (--add-dir project_root)
  - Sees ALL outstanding canonical findings (across all services)
  - Can cross-service edits (e.g., backend API change + frontend
    consumer change in one coherent fix)
  - Bash access to docker compose so it can verify with actual
    pytest in real containers

The user's mental model: one smart debugger, sequential, with full
context. Coders frame fast; debugger does the deep work.

Returns a structured result + a list of touched files. Caller (v5
loop) re-runs the resolution check after this returns to verify.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.canonical_findings.types import CanonicalFinding


_ALLOWED_TOOLS = ["Edit", "Write", "Read", "Bash", "Glob", "Grep"]


_SYSTEM_PROMPT = """You are a MILESTONE-SCOPED debugger. The
pipeline's structured fix-loop couldn't resolve the canonical
findings below; your job is to make it happen with full tool access.

# Scope
You can read + edit ANY file in the project. Cross-service fixes
are fair game (e.g., fix a backend route + the frontend that calls
it as one coherent change). Stay within the milestone's scope —
don't refactor unrelated code.

# Toolbox
- **Edit / Write / Read / Glob / Grep**: file ops on the workspace
  (current dir = project root).
- **Bash**: ``docker compose -f <compose> exec -T <svc> python -m
  pytest tests/...`` to verify fixes in the actual container.
  ``docker compose logs --tail N <svc>`` for upstream errors. Use
  these aggressively — don't claim a fix without verifying.

# Findings to resolve
Each canonical finding has a stable id, a summary, a file_hint
(usually the right place to look), and detail (the evidence the
reviewer flagged). Resolve as many as you can; the resolution
check downstream will judge each one.

# When done
Stop using tools and emit a final line:

  DEBUGGER_DONE: status=<clean|partial>, files_touched=[a.py, b.py, ...]

Status:
  - ``clean`` — you believe all findings are resolved
  - ``partial`` — some are resolved, others left for the next iter

# Constraints
- Do NOT introduce new code unrelated to the findings.
- Do NOT remove tests; fix the underlying code instead.
- Do NOT silently swallow exceptions to make tests pass.
- Imports must actually resolve in the project's environment
  (verify with Bash if uncertain).
"""


def _truncate(text: str, max_chars: int = 6000) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2 - 50
    tail = max_chars - head - 100
    return (
        text[:head]
        + f"\n\n...[truncated {len(text) - head - tail} chars]...\n\n"
        + text[-tail:]
    )


class PerMilestoneDebuggerError(Exception):
    """Subprocess returned non-zero or could not parse final status."""


class PerMilestoneDebuggerResult(BaseModel):
    """Output of one debugger invocation."""

    clean: bool = Field(
        ...,
        description="True when debugger reported all findings resolved.",
    )
    files_touched: List[str] = Field(default_factory=list)
    wall_s: float = 0.0
    halt_reason: str = Field(
        default="",
        description="Non-empty when not clean (timeout, exit_N, partial).",
    )
    raw_tail: str = Field(
        default="",
        description="Last 1KB of debugger output — for diagnosis.",
    )


class PerMilestoneDebugger:
    """One smart tool-loop debugger per milestone repair iter."""

    def __init__(
        self,
        *,
        project_root: Path,
        compose_path: Optional[str] = None,
        timeout_seconds: int = 3000,
        on_status: Optional[Callable[[str], None]] = None,
        command: str = "claude",
        additional_args: Optional[List[str]] = None,
        max_file_chars: int = 6000,
        max_findings: int = 30,
    ):
        self._project_root = Path(project_root)
        self._compose_path = compose_path or ""
        self._timeout_s = float(timeout_seconds)
        self._on_status = on_status
        self._command = command
        self._additional_args = list(additional_args or [])
        self._max_file_chars = max_file_chars
        self._max_findings = max_findings

        if shutil.which(self._command) is None:
            raise PerMilestoneDebuggerError(
                f"PerMilestoneDebugger: ``{self._command}`` not on PATH."
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
        milestone_name: str,
        findings: List[CanonicalFinding],
        current_files: Optional[dict] = None,
    ) -> PerMilestoneDebuggerResult:
        """Run the milestone-scoped tool-loop debugger."""
        t0 = time.time()
        if not findings:
            self._log(
                "PerMilestoneDebugger: no findings to debug — nothing to do"
            )
            return PerMilestoneDebuggerResult(
                clean=True, files_touched=[], wall_s=0.0,
            )

        self._log(
            f"PerMilestoneDebugger[{milestone_name}]: starting "
            f"({len(findings)} finding(s), timeout={self._timeout_s:.0f}s)"
        )

        prompt = self._build_prompt(
            milestone_name=milestone_name,
            findings=findings,
            current_files=current_files or {},
        )

        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", _SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(_ALLOWED_TOOLS),
            "--add-dir", str(self._project_root),
        ] + self._additional_args

        env = os.environ.copy()

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                cwd=str(self._project_root),
                env=env,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            self._log(
                f"PerMilestoneDebugger[{milestone_name}]: TIMEOUT after "
                f"{elapsed:.1f}s — partial work may remain on disk"
            )
            return PerMilestoneDebuggerResult(
                clean=False, files_touched=[],
                wall_s=elapsed, halt_reason="timeout",
            )

        elapsed = time.time() - t0
        self._log(
            f"PerMilestoneDebugger[{milestone_name}]: subprocess done "
            f"in {elapsed:.1f}s (exit {proc.returncode})"
        )

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout)[:1000]
            return PerMilestoneDebuggerResult(
                clean=False, files_touched=[],
                wall_s=elapsed,
                halt_reason=f"exit_{proc.returncode}",
                raw_tail=tail,
            )

        # Parse claude --print's JSON envelope.
        try:
            payload = json.loads(proc.stdout)
            result_text = payload.get("result") or ""
        except json.JSONDecodeError:
            result_text = proc.stdout

        status = "partial"
        files_touched: List[str] = []
        for line in result_text.splitlines():
            line = line.strip()
            if line.startswith("DEBUGGER_DONE:"):
                if "status=clean" in line:
                    status = "clean"
                elif "status=partial" in line:
                    status = "partial"
                if "files_touched=[" in line:
                    head = line.split("files_touched=[", 1)[1]
                    inner = head.split("]", 1)[0]
                    files_touched = [
                        s.strip().strip("'\"")
                        for s in inner.split(",") if s.strip()
                    ]

        clean = status == "clean"
        self._log(
            f"PerMilestoneDebugger[{milestone_name}]: status={status}, "
            f"files_touched={files_touched}"
        )
        return PerMilestoneDebuggerResult(
            clean=clean,
            files_touched=files_touched,
            wall_s=elapsed,
            halt_reason="" if clean else "partial",
            raw_tail=result_text[-1000:],
        )

    def _build_prompt(
        self, *, milestone_name: str,
        findings: List[CanonicalFinding],
        current_files: dict,
    ) -> str:
        sections: List[str] = []
        sections.append(f"## Milestone\n\n`{milestone_name}`\n")

        if self._compose_path:
            sections.append(
                f"## Container access\n\n"
                f"- compose: `{self._compose_path}`\n"
                f"- run pytest in a service: ``docker compose -f "
                f"{self._compose_path} exec -T <svc> python -m pytest "
                f"tests/test_X.py -x``\n"
            )

        sections.append(
            f"\n## Canonical findings to resolve ({len(findings)} total)\n"
        )
        truncated = findings[: self._max_findings]
        for f in truncated:
            sections.append(f"### `{f.id}`")
            sections.append(f"- source: {f.source}")
            sections.append(f"- priority: {f.priority}")
            if f.capability_id:
                sections.append(f"- capability: `{f.capability_id}`")
            if f.file_hint:
                sections.append(f"- file_hint: `{f.file_hint}`")
            sections.append(f"- summary: {f.summary}")
            if f.detail:
                sections.append(f"- detail: {f.detail[:500]}")
            sections.append(f"- status: {f.status}")
            sections.append("")
        if len(findings) > self._max_findings:
            sections.append(
                f"_({len(findings) - self._max_findings} more findings "
                f"truncated — focus on the ones above first)_\n"
            )

        if current_files:
            sections.append("\n## Current code (selected files)\n")
            for path, content in list(current_files.items())[:10]:
                sections.append(f"### `{path}`")
                sections.append("```")
                sections.append(_truncate(content, self._max_file_chars))
                sections.append("```")
                sections.append("")

        sections.append(
            "\n## Your job\n\n"
            "Investigate each finding, fix it using Edit/Write, and "
            "verify fixes with Bash (run pytest in the container). "
            "When done — even partially — emit:\n\n"
            "  DEBUGGER_DONE: status=<clean|partial>, files_touched=[...]"
        )
        return "\n".join(sections)
