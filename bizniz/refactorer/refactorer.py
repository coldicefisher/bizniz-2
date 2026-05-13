"""Refactorer — cross-service dedup + extract-to-shared agent.

Stage 2b minimum-viable: a single Claude Code CLI invocation rooted
at the project (so all service workspaces are visible). The system
prompt teaches the model what to look for, where to put shared code,
and how to verify after each extraction.

This is intentionally low-ceremony: no pre-pass duplication detector,
no per-candidate dispatch. The LLM does the analysis + edits + test
runs end-to-end in one tool-loop session. If quality is poor on real
projects, the next iteration adds structure (deterministic candidate
finding, per-candidate dispatch, escalation).

Result shape mirrors CoderResult-ish so the RefactorPhase artifact
is structured and inspectable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from bizniz.architect.types import SystemArchitecture
from bizniz.planner.types import Milestone


_DEFAULT_TIMEOUT_S = 3600.0  # 1 hour ceiling for a full refactor pass

# Same tools the Coder uses — Read/Glob/Grep for discovery, Edit/Write
# for changes, Bash to run tests in the compose stack.
_ALLOWED_TOOLS = ["Edit", "Write", "Read", "Bash", "Glob", "Grep"]


class RefactorerError(Exception):
    pass


class RefactorerExtraction(BaseModel):
    name: str = ""
    shared_path: str = ""
    consumers: List[str] = Field(default_factory=list)
    before: str = ""
    after: str = ""
    tests_passed: bool = False


class RefactorerResult(BaseModel):
    status: str = "no_op"   # passed | partial | failed | no_op
    extractions: List[RefactorerExtraction] = Field(default_factory=list)
    skipped: List[Dict] = Field(default_factory=list)
    summary: str = ""
    notes: List[str] = Field(default_factory=list)
    duration_s: float = 0.0


class Refactorer:
    """Cross-service refactor pass via Claude Code CLI.

    Construction is config-only — no per-call state. ``run()`` is
    a single subprocess invocation per milestone (the Phase
    constructs and calls it; resume gates skip on completion).
    """

    def __init__(
        self,
        project_root: Path,
        compose_path: str,
        timeout_seconds: int = int(_DEFAULT_TIMEOUT_S),
        command: str = "claude",
        on_status: Optional[Callable[[str], None]] = None,
        additional_args: Optional[List[str]] = None,
    ):
        self._project_root = Path(project_root)
        self._compose_path = compose_path
        self._timeout_s = float(timeout_seconds)
        self._command = command
        self._on_status = on_status
        self._additional_args = list(additional_args or [])

        if shutil.which(self._command) is None:
            raise RefactorerError(
                f"Refactorer: {self._command!r} not on PATH. "
                f"Install Claude Code CLI."
            )

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    def run(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        is_final_milestone: bool,
    ) -> RefactorerResult:
        """Single Claude invocation rooted at the project. Returns
        a parsed RefactorerResult."""
        from bizniz.refactorer.prompts import SYSTEM_PROMPT

        scope = "FINAL-MILESTONE" if is_final_milestone else "MID-PROJECT"
        self._log(
            f"Refactorer ({scope}): starting refactor pass for milestone "
            f"'{milestone.name}' (M{milestone.sequence_index + 1})"
        )

        user_prompt = self._build_user_prompt(
            milestone, architecture, is_final_milestone,
        )

        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(_ALLOWED_TOOLS),
            "--add-dir", str(self._project_root),
        ] + self._additional_args

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                input=user_prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                cwd=str(self._project_root),
            )
        except subprocess.TimeoutExpired as e:
            self._log(
                f"Refactorer: timed out after {self._timeout_s:.0f}s"
            )
            return RefactorerResult(
                status="partial",
                summary=f"Timed out after {self._timeout_s:.0f}s",
                notes=["refactorer subprocess timeout"],
                duration_s=time.time() - t0,
            )
        except FileNotFoundError as e:
            return RefactorerResult(
                status="failed",
                summary=f"claude binary missing: {e}",
                duration_s=time.time() - t0,
            )

        elapsed = time.time() - t0
        self._log(
            f"Refactorer: subprocess done in {elapsed:.1f}s "
            f"(exit {proc.returncode})"
        )

        if proc.returncode != 0:
            return RefactorerResult(
                status="failed",
                summary=(
                    f"claude --print exited {proc.returncode}: "
                    f"{(proc.stderr or proc.stdout)[:300]}"
                ),
                duration_s=elapsed,
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return RefactorerResult(
                status="failed",
                summary=f"non-JSON CLI output: {proc.stdout[:200]}",
                duration_s=elapsed,
            )

        if payload.get("is_error"):
            return RefactorerResult(
                status="failed",
                summary=f"is_error=true: {payload.get('result', '')[:300]}",
                duration_s=elapsed,
            )

        result_text = payload.get("result") or ""
        parsed = self._parse_result(result_text)
        parsed.duration_s = elapsed
        return parsed

    # ── Internals ──────────────────────────────────────────────────────

    def _build_user_prompt(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        is_final_milestone: bool,
    ) -> str:
        services_block = "\n".join(
            f"  - {s.name} ({s.framework}/{s.language}) "
            f"at ./{s.workspace_name}/"
            for s in architecture.services
            if (s.service_type or "").lower() in (
                "backend", "frontend", "worker", "consumer",
            )
        )
        scope_note = (
            "This is the FINAL milestone — do a comprehensive pass."
            if is_final_milestone else
            f"This runs at the boundary of milestone '{milestone.name}'. "
            f"Focus on duplication introduced by the recent milestones; "
            f"future milestones will get their own refactor passes."
        )
        return (
            f"PROJECT: {architecture.project_name}\n"
            f"PROJECT ROOT: {self._project_root}\n"
            f"DOCKER COMPOSE: {self._compose_path}\n\n"
            f"MILESTONE JUST SHIPPED:\n"
            f"  Name: {milestone.name}\n"
            f"  Description: {milestone.problem_slice[:500]}\n\n"
            f"SERVICES:\n{services_block}\n\n"
            f"SCOPE: {scope_note}\n\n"
            f"Run the refactor pass per the workflow in the system "
            f"prompt. End with the final JSON result object."
        )

    def _parse_result(self, text: str) -> RefactorerResult:
        """Extract the trailing JSON object, validate against
        RefactorerResult, fall back to no_op on parse failures."""
        if not text:
            return RefactorerResult(
                status="no_op",
                summary="empty response from refactorer",
            )
        # Try parsing as-is first (model followed instructions).
        candidate = text.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            try:
                return RefactorerResult.model_validate_json(candidate)
            except Exception:
                pass
        # Try extracting trailing balanced JSON.
        extracted = _extract_trailing_json(text)
        if extracted:
            try:
                return RefactorerResult.model_validate_json(extracted)
            except Exception:
                pass
        # Try fenced JSON.
        fenced = _extract_fenced_json(text)
        if fenced:
            try:
                return RefactorerResult.model_validate_json(fenced)
            except Exception:
                pass
        # Give up — record what we saw.
        return RefactorerResult(
            status="no_op",
            summary=(
                "refactorer did not emit a parseable result; "
                f"raw head: {text[:200]}"
            ),
        )


def _extract_trailing_json(text: str) -> Optional[str]:
    """Scan from end-of-text for a balanced ``{...}`` JSON object."""
    depth = 0
    end = None
    for i in range(len(text) - 1, -1, -1):
        c = text[i]
        if c == "}":
            if end is None:
                end = i
            depth += 1
        elif c == "{":
            depth -= 1
            if depth == 0 and end is not None:
                return text[i:end + 1]
    return None


def _extract_fenced_json(text: str) -> Optional[str]:
    m = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL)
    return m.group(1) if m else None
