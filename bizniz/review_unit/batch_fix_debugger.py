"""``BatchFixDebugger`` — v3 spec phase 3 agent.

Consumes a unified ``FindingsReport`` (covering static checks +
pytest + QualityEngineer + CodeReviewer in one stream) and tries to
fix as many findings as possible in a single agent session. Same
tool surface as today's ``ClaudeCliDebugger`` (Read/Edit/Write/Bash/
Glob/Grep + MCP), preserved per the spec.

Differences from today's per-failure debugger:

1. **Input is the full findings inventory**, not just test output. The
   agent sees mypy + ruff + tsc + pytest + QE + CR at once.
2. **Batch-fix orientation**: the prompt explicitly tells the agent
   to look for cross-cutting root causes — a single fix often
   resolves multiple findings (e.g., adding a missing field on
   ``RecipeOut`` clears mypy + pytest + QE all at once).
3. **ProgressTracker-bounded outer loop**: caller wraps this in a
   loop that re-runs the review unit between iterations; stops on
   stall (findings count not dropping) or clean (count = 0).

This module exposes the single-call agent. The outer review-unit
orchestration (run static checks + pytest + QE + CR in parallel,
build the report, decide stall, loop) lives separately.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.review_unit.types import FindingsReport, ProgressVerdict


_ALLOWED_TOOLS = ["Edit", "Write", "Read", "Bash", "Glob", "Grep"]


_BATCH_FIX_SYSTEM_PROMPT = """\
You are a batch-fix debugger for a Bizniz pipeline. The review unit
just ran (static checks + pytest + QualityEngineer + CodeReviewer
all in parallel) and produced a UNIFIED FINDINGS REPORT. Your job
is to fix as many findings as you can in ONE pass.

# Why batch-fix matters

Today's per-failure debugger fixes ONE thing per iteration. The
review unit then re-runs and the next failure surfaces — repeat
many times. That's slow.

You have the WHOLE inventory in front of you. Look for cross-
cutting root causes:

  - "RecipeOut missing field `tags`" (mypy)
    + "test_me asserts response.json()['tags']" (pytest)
    + "response missing tags per spec" (QE)
    → ONE fix: add the field. Three findings clear.

  - "unused import `JWTError`" (ruff)
    + "missing JWT error handling" (CR)
    → ONE fix: add the error handler using the import.

# Your toolbox

- **Read / Edit / Write**: file ops on the workspace (current dir).
- **Bash**: run commands inside service containers via
  ``docker compose -f <path> exec -T <svc> <cmd>``, query logs,
  hit endpoints with curl.
- **Glob / Grep**: filesystem search.

# Workflow

1. **Skim the full findings report first.** Don't dive into the
   first finding — look for shared root causes.
2. **Cluster related findings** mentally. Same file + similar
   message + multiple sources = likely one fix.
3. **Read the relevant files** before editing — symbol_validator
   findings tell you what's broken in the import graph; pytest
   tracebacks show runtime behavior. Don't fix blind.
4. **Apply fixes in priority order**: critical > high > medium > low.
   If you have time, address all severities; if pressed, leave
   ``low`` findings for the next iteration.
5. **One fix at a time per file**, but as many files as the
   findings list demands. The outer loop will re-run the review
   unit and surface anything left.

# Output format

After applying fixes via Edit/Write, return ONE valid JSON object
matching the provided schema. Summarize what you changed and which
findings each change addresses. The fixes themselves are already
on disk — the JSON is the audit trail.
"""


_BATCH_FIX_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "batch_fix_result",
        "schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One-paragraph human-readable summary of what was fixed and what wasn't.",
                },
                "fixes_applied": {
                    "type": "array",
                    "description": "One entry per logical fix, with the findings it addresses.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "files_touched": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Workspace-relative paths the fix edited.",
                            },
                            "description": {
                                "type": "string",
                                "description": "One-line description of the fix.",
                            },
                            "addresses_fingerprints": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Fingerprints of findings this fix is intended to clear.",
                            },
                        },
                        "required": [
                            "files_touched", "description",
                            "addresses_fingerprints",
                        ],
                        "additionalProperties": False,
                    },
                },
                "skipped_fingerprints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Findings the agent deliberately did NOT address (out of scope, ambiguous, etc.) with a reason inline in summary.",
                },
            },
            "required": [
                "summary", "fixes_applied", "skipped_fingerprints",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


# ── Return type ───────────────────────────────────────────────────


class AppliedFix(BaseModel):
    files_touched: List[str]
    description: str
    addresses_fingerprints: List[str]


class BatchFixResult(BaseModel):
    summary: str = ""
    fixes_applied: List[AppliedFix] = Field(default_factory=list)
    skipped_fingerprints: List[str] = Field(default_factory=list)
    wall_s: float = 0.0
    raw_session_id: Optional[str] = None


class BatchFixDebuggerError(Exception):
    pass


# ── Agent ─────────────────────────────────────────────────────────


class BatchFixDebugger:
    """Consumes a unified ``FindingsReport`` + workspace path; runs one
    ``claude --print`` session with full tools; emits a structured
    summary of fixes applied (files are already on disk after).
    """

    def __init__(
        self,
        workspace_root: Path,
        on_status: Optional[Callable[[str], None]] = None,
        command: str = "claude",
        timeout_seconds: int = 1800,
        additional_args: Optional[List[str]] = None,
        model_name: str = "claude-cli:claude-opus-4-7",
    ):
        self._workspace_root = Path(workspace_root)
        self._on_status = on_status
        self._command = command
        self._timeout_s = float(timeout_seconds)
        self._model_name = model_name

        from bizniz.clients.claude_cli.model_name import parse_claude_cli_model
        _label, model_args = parse_claude_cli_model(model_name)
        self._additional_args = list(additional_args or []) + model_args

        if shutil.which(self._command) is None:
            raise BatchFixDebuggerError(
                f"BatchFixDebugger: ``{self._command}`` not on PATH."
            )

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

    # ── Public ────────────────────────────────────────────────────

    def run(
        self,
        *,
        report: FindingsReport,
        compose_path: Optional[str] = None,
        service_name: Optional[str] = None,
    ) -> BatchFixResult:
        """Run one batch-fix pass on the workspace. The agent reads the
        findings report + workspace files, applies fixes via Edit/Write
        directly, and returns a summary of what it did.

        Re-running the review unit afterward is the caller's job; the
        ``ProgressTracker`` lives outside this agent.
        """
        self._log(
            f"BatchFixDebugger: {report.count} findings "
            f"({report.critical_count} critical, {report.high_count} high)"
        )

        prompt = self._build_user_prompt(
            report=report,
            compose_path=compose_path,
            service_name=service_name,
        )

        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", _BATCH_FIX_SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(_ALLOWED_TOOLS),
            "--add-dir", str(self._workspace_root),
        ] + self._additional_args

        env = os.environ.copy()

        from bizniz.clients.claude_cli.retry import run_with_429_retry
        t0 = time.time()
        try:
            proc = run_with_429_retry(
                cmd,
                input=prompt,
                timeout=self._timeout_s,
                cwd=str(self._workspace_root),
                env=env,
                log_prefix=f"[BatchFixDebugger]",
            )
        except subprocess.TimeoutExpired as e:
            raise BatchFixDebuggerError(
                f"claude --print timed out after {self._timeout_s:.0f}s"
            ) from e
        except FileNotFoundError as e:
            raise BatchFixDebuggerError(f"claude not found: {e}") from e
        except RuntimeError as e:
            raise BatchFixDebuggerError(str(e)) from e

        elapsed = time.time() - t0
        self._log(
            f"BatchFixDebugger: subprocess done in {elapsed:.1f}s "
            f"(exit {proc.returncode})"
        )

        if proc.returncode != 0:
            raise BatchFixDebuggerError(
                f"claude --print exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[:400]}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise BatchFixDebuggerError(
                f"claude --print returned non-JSON: {e}\n"
                f"stdout head: {proc.stdout[:400]}"
            ) from e

        if payload.get("is_error"):
            raise BatchFixDebuggerError(
                f"claude returned is_error=true: "
                f"{payload.get('result', '')[:400]}"
            )

        result_text = payload.get("result") or ""
        session_id = payload.get("session_id") or str(uuid.uuid4())

        parsed = self._parse_result_text(result_text)
        parsed.wall_s = elapsed
        parsed.raw_session_id = session_id
        return parsed

    # ── Internals ─────────────────────────────────────────────────

    def _build_user_prompt(
        self,
        *,
        report: FindingsReport,
        compose_path: Optional[str],
        service_name: Optional[str],
    ) -> str:
        sections: List[str] = []

        sections.append(f"# Unified findings report (iteration {report.iteration})")
        sections.append(report.summary_line())
        sections.append("")

        # Group by severity descending — critical first.
        sev_order = ["critical", "high", "medium", "low"]
        grouped = report.by_severity()
        for sev in sev_order:
            items = grouped[sev]
            if not items:
                continue
            sections.append(f"## {sev.upper()} ({len(items)})")
            for f in items:
                loc = ""
                if f.file_path:
                    loc = f.file_path
                    if f.line is not None:
                        loc += f":{f.line}"
                src = f"[{f.source}]"
                tag = f.fingerprint
                sections.append(f"- **{src} {tag}** {loc}")
                sections.append(f"  {f.message}")
                if f.suggested_fix:
                    sections.append(f"  *suggested fix:* {f.suggested_fix}")
                if f.raw and len(f.raw) < 400:
                    sections.append(f"  ```")
                    sections.append(f"  {f.raw}")
                    sections.append(f"  ```")
            sections.append("")

        if compose_path:
            sections.append("# Compose stack")
            sections.append(
                f"The dev stack is at ``{compose_path}``. Use "
                f"``docker compose -f {compose_path} exec -T <svc> ...`` "
                f"for runtime probes. Target service: "
                f"``{service_name or '(any)'}``."
            )
            sections.append("")

        sections.append("# Your job")
        sections.append(
            "Apply fixes via Edit/Write directly. Cluster findings "
            "that share root causes. Output the structured summary "
            "JSON at the end."
        )

        return "\n".join(sections)

    def _parse_result_text(self, text: str) -> BatchFixResult:
        """Pull the structured JSON out of Claude's final response."""
        text = text.strip()
        # Try the optimistic path first: text IS the JSON object.
        try:
            obj = json.loads(text)
            return self._envelope_to_result(obj)
        except json.JSONDecodeError:
            pass
        # Fallback: extract the last {...} block.
        start = text.rfind("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            chunk = text[start:end + 1]
            try:
                obj = json.loads(chunk)
                return self._envelope_to_result(obj)
            except json.JSONDecodeError:
                pass
        raise BatchFixDebuggerError(
            f"could not parse structured JSON from Claude's response. "
            f"Head: {text[:400]}"
        )

    @staticmethod
    def _envelope_to_result(obj: dict) -> BatchFixResult:
        fixes = [AppliedFix(**f) for f in (obj.get("fixes_applied") or [])]
        return BatchFixResult(
            summary=obj.get("summary", ""),
            fixes_applied=fixes,
            skipped_fingerprints=list(obj.get("skipped_fingerprints") or []),
        )


# ── ProgressTracker helper ────────────────────────────────────────


def compute_progress_verdict(
    *,
    prior: Optional[FindingsReport],
    current: FindingsReport,
    stall_counter: int,
    stall_threshold: int = 5,
) -> ProgressVerdict:
    """Decide whether the latest iteration made progress, stalled, or
    regressed. Stall counter resets on progress, increments on
    stall/regress."""
    prior_count = prior.count if prior is not None else current.count
    cur_count = current.count

    if cur_count == 0:
        return ProgressVerdict(
            verdict="clean",
            prior_count=prior_count,
            current_count=cur_count,
            stall_counter=0,
            stall_threshold=stall_threshold,
            should_continue=False,
            should_escalate_tier=False,
        )
    if cur_count < prior_count:
        return ProgressVerdict(
            verdict="progress",
            prior_count=prior_count,
            current_count=cur_count,
            stall_counter=0,
            stall_threshold=stall_threshold,
            should_continue=True,
            should_escalate_tier=False,
        )
    if cur_count > prior_count:
        new_stall = stall_counter + 1
        return ProgressVerdict(
            verdict="regress",
            prior_count=prior_count,
            current_count=cur_count,
            stall_counter=new_stall,
            stall_threshold=stall_threshold,
            should_continue=new_stall < stall_threshold,
            should_escalate_tier=new_stall >= stall_threshold,
        )
    # equal — stall
    new_stall = stall_counter + 1
    return ProgressVerdict(
        verdict="stall",
        prior_count=prior_count,
        current_count=cur_count,
        stall_counter=new_stall,
        stall_threshold=stall_threshold,
        should_continue=new_stall < stall_threshold,
        should_escalate_tier=new_stall >= stall_threshold,
    )
