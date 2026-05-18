"""Generalized phase-recovery dispatcher (D14, 2026-05-17).

Pulls the plumbing out of ``SmokeRecovery`` so other phases
(refactor, document) can reuse the exact same shape with their own
focused system prompts.

**Shape:** spawn a single CLI session, given a failure-context
prompt and the FULL discovery + edit + bash toolset. The session
fixes whatever it can, returns. The caller verifies via a phase
re-run + a ``ProgressTracker``.

**Why a full discovery toolset by default, even on Claude CLI:** the
Claude CLI auto-loads files from ``--add-dir``, but our prompts
shouldn't assume that. When this same recovery pattern needs to
work against Gemini or OpenAI backends (which require explicit
tool calls for file access), the prompts already use
``view_file`` / ``list_directory`` / ``search_files`` /
``search_imports`` / Grep — and the dispatcher's job is just to
make those tools available. Same prompts, same recovery shape,
different backend.

**Per-phase subclasses** override:
- ``label`` — used in log lines + recovery results
- ``system_prompt`` — focused for the failure type (smoke, refactor,
  docs, ...)
- ``build_user_prompt(...)`` — turns failure context into the LLM
  user message

The base class owns the subprocess invocation, JSON parsing,
timeout handling, action extraction, and self-reported-ok signal.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from pydantic import BaseModel, Field


# Default tool set — full file discovery + edit + bash. Same shape
# the Coder + AgenticDebugger use. Designed to be portable: the
# string names map 1:1 to Claude CLI's allowed-tools flags AND to
# the bizniz tool-loop tool registry for non-Claude backends.
DEFAULT_RECOVERY_TOOLS: Tuple[str, ...] = (
    "Bash", "Read", "Edit", "Write", "Glob", "Grep",
)

DEFAULT_TIMEOUT_S: float = 900.0  # 15 min — generous but bounded


class PhaseRecoveryResult(BaseModel):
    """Outcome of one recovery attempt. Same shape for every
    subclass — the caller's ProgressTracker loop just cares about
    succeeded + actions_taken."""
    attempted: bool = False
    succeeded: bool = False
    summary: str = ""
    actions_taken: List[str] = Field(default_factory=list)
    elapsed_s: float = 0.0
    raw_response: str = ""


class AgenticPhaseRecovery:
    """Base for single-session recovery agents.

    Concrete subclasses set ``label`` + ``system_prompt`` (class
    attributes) and override ``build_user_prompt(**ctx)`` to format
    the failure context into a user message. The base class drives
    the Claude CLI subprocess, parses the result, and returns a
    uniform ``PhaseRecoveryResult``.

    A subclass with no overrides + a custom prompt is usable too —
    just pass ``label`` and ``system_prompt`` to ``__init__``.
    """

    #: Short label used in log lines + status messages.
    label: str = "PhaseRecovery"

    #: Focused system prompt — set by subclass OR by __init__.
    system_prompt: str = ""

    def __init__(
        self,
        *,
        project_root: Path,
        label: Optional[str] = None,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[Tuple[str, ...]] = None,
        command: str = "claude",
        timeout_seconds: float = DEFAULT_TIMEOUT_S,
        on_status: Optional[Callable[[str], None]] = None,
        fallback_model: Optional[str] = None,
    ) -> None:
        self._project_root = Path(project_root)
        # __init__ args win over class attributes (so callers can
        # parameterize without subclassing).
        if label is not None:
            self.label = label
        if system_prompt is not None:
            self.system_prompt = system_prompt
        self._allowed_tools = tuple(allowed_tools or DEFAULT_RECOVERY_TOOLS)
        self._command = command
        self._timeout_s = timeout_seconds
        self._on_status = on_status
        self._fallback_model = (
            fallback_model or os.environ.get("BIZNIZ_CLAUDE_FALLBACK_MODEL")
        )

    # ── Subclass hook ──────────────────────────────────────────────

    def build_user_prompt(self, **context) -> str:
        """Subclasses turn arbitrary failure context into the user
        message Claude receives. Default implementation just dumps
        the context as a labeled block — usable but uninspired."""
        lines = [f"{self.label} — recovery requested.", ""]
        for k, v in context.items():
            lines.append(f"## {k}")
            lines.append("")
            lines.append(str(v))
            lines.append("")
        return "\n".join(lines)

    # ── Public ─────────────────────────────────────────────────────

    def recover(self, **context) -> PhaseRecoveryResult:
        """Dispatch one CLI session to attempt recovery.

        Returns a ``PhaseRecoveryResult``. Caller re-runs the
        owning phase (smoke, refactor, docs) and decides whether
        to keep iterating (``ProgressTracker``) or halt.
        """
        if shutil.which(self._command) is None:
            self._log(
                f"{self.label}: '{self._command}' binary not on PATH; "
                f"skipping recovery"
            )
            return PhaseRecoveryResult(
                attempted=False, succeeded=False,
                summary=f"{self._command} binary not available",
            )

        user_prompt = self.build_user_prompt(**context)
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", self.system_prompt,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(self._allowed_tools),
            "--add-dir", str(self._project_root),
        ]
        if self._fallback_model:
            cmd.extend(["--fallback-model", self._fallback_model])

        ctx_preview = ", ".join(f"{k}={_short(v)}" for k, v in context.items())
        self._log(f"{self.label}: dispatching ({ctx_preview})")

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, input=user_prompt, capture_output=True, text=True,
                timeout=self._timeout_s, cwd=str(self._project_root),
            )
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            self._log(f"{self.label}: timed out after {elapsed:.0f}s")
            return PhaseRecoveryResult(
                attempted=True, succeeded=False,
                summary=f"recovery timed out after {self._timeout_s:.0f}s",
                elapsed_s=elapsed,
            )
        except FileNotFoundError as e:
            return PhaseRecoveryResult(
                attempted=False, succeeded=False,
                summary=f"{self._command} binary missing at runtime: {e}",
            )
        elapsed = time.time() - t0

        if proc.returncode != 0:
            self._log(
                f"{self.label}: {self._command} exited "
                f"{proc.returncode}: {(proc.stderr or '')[:200]}"
            )
            return PhaseRecoveryResult(
                attempted=True, succeeded=False,
                summary=f"{self._command} exited {proc.returncode}",
                elapsed_s=elapsed,
                raw_response=(proc.stdout or "")[:2000],
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return PhaseRecoveryResult(
                attempted=True, succeeded=False,
                summary="non-JSON CLI output",
                elapsed_s=elapsed,
                raw_response=(proc.stdout or "")[:2000],
            )
        if payload.get("is_error"):
            return PhaseRecoveryResult(
                attempted=True, succeeded=False,
                summary=f"{self._command} is_error=true",
                elapsed_s=elapsed,
                raw_response=(payload.get("result") or "")[:2000],
            )

        result_text = payload.get("result") or ""
        actions = _extract_action_lines(result_text)
        self_reported_ok = _self_reported_success(result_text)
        self._log(
            f"{self.label}: returned in {elapsed:.1f}s — "
            f"{len(actions)} action(s); self_reported_ok={self_reported_ok}"
        )
        return PhaseRecoveryResult(
            attempted=True, succeeded=self_reported_ok,
            summary=result_text[:400],
            actions_taken=actions,
            elapsed_s=elapsed,
            raw_response=result_text[:4000],
        )

    # ── Internals ──────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass


def _short(value) -> str:
    """Compact a context value for the dispatch log line."""
    s = str(value)
    if len(s) <= 60:
        return s
    return s[:57] + "…"


def _extract_action_lines(result_text: str) -> List[str]:
    """Pull ``ACTION:`` lines the model emits, for diagnostics.
    Best-effort — empty list if model didn't emit the convention."""
    out: List[str] = []
    for line in result_text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("ACTION:"):
            out.append(stripped[7:].strip()[:200])
    return out


def _self_reported_success(result_text: str) -> bool:
    """Look for the ``RECOVERY SUCCESS`` sentinel. The harness still
    verifies by re-running the phase; this is just a soft signal."""
    return (
        "RECOVERY SUCCESS" in result_text
        or "recovery succeeded" in result_text.lower()
    )
