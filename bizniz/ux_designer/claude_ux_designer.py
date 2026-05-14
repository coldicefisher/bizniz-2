"""ClaudeUXDesigner — UXDesigner variant that uses Claude Code CLI
for vision evaluation instead of inline-image API calls.

Why: the Claude CLI's Read tool is multimodal — it reads PNG files
directly from disk. We already write screenshots to
``{workspace}/screenshots/*.png`` in the parent class. Pointing
Claude at that directory with ``--add-dir`` gives it full access
to evaluate them as it would in interactive Claude Code, and the
marginal cost is $0 on a Max plan.

Architecture: subclass of UXDesigner. Reuses screenshot capture,
script generation (via ClaudeCliClient.get_text), and the fix
dispatch (ClaudeCliCoder, via the inherited coder_factory).
Overrides ``_evaluate_screenshots`` to call ``claude --print``
against the screenshots directory instead of sending image bytes
inline to a vision API.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.architect.types import ServiceDefinition
from bizniz.ux_designer.prompts import EVALUATE_PROMPT, EVALUATE_SCHEMA
from bizniz.ux_designer.ux_designer import UXDesigner, _log


_DEFAULT_TIMEOUT_S = 600.0
_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]


def _eval_instructions(schema_str: str) -> str:
    """The user-side instructions Claude gets each evaluation call."""
    return (
        f"{EVALUATE_PROMPT}\n\n"
        f"INSTRUCTIONS:\n"
        f"1. The directory you have access to contains the screenshots "
        f"of the application. Use Glob or list_directory to find every "
        f"PNG.\n"
        f"2. Use the Read tool on each PNG to view it.\n"
        f"3. After reviewing every screenshot, return a single JSON "
        f"object matching this schema — no markdown fences, no prose, "
        f"nothing else:\n\n"
        f"SCHEMA:\n{schema_str}\n"
    )


class ClaudeUXDesigner(UXDesigner):
    """UX review using Claude Code CLI for vision eval.

    Construction surface mirrors UXDesigner. ``vision_client`` is
    typed as anything with a ``get_text`` method (we use it to
    generate the Playwright screenshot script — text-only). For
    image evaluation we shell out to the CLI directly.
    """

    def __init__(
        self,
        vision_client,
        coder_factory: Optional[Callable] = None,
        on_status: Optional[Callable[[str], None]] = None,
        max_fix_iterations: int = 2,
        acceptable_score: int = 6,
        command: str = "claude",
        timeout_seconds: int = int(_DEFAULT_TIMEOUT_S),
        additional_args: Optional[List[str]] = None,
    ):
        # Parent sets ``self._vision._caller_agent = "ux_designer"`` —
        # ClaudeCliClient accepts attribute assignment so this works
        # for both Gemini and Claude clients without special-casing.
        super().__init__(
            vision_client=vision_client,
            coder_factory=coder_factory,
            on_status=on_status,
            max_fix_iterations=max_fix_iterations,
            acceptable_score=acceptable_score,
        )
        self._command = command
        self._timeout_s = float(timeout_seconds)
        self._additional_args = list(additional_args or [])
        if shutil.which(self._command) is None:
            # Not fatal — let the override raise at first use so unit
            # tests can construct a designer without a real CLI.
            _log(
                self._on_status,
                f"ClaudeUXDesigner: {self._command!r} not on PATH at "
                f"construction (will fail at eval time)"
            )

    # ── Override: vision eval via Claude CLI ──────────────────────────

    def _evaluate_screenshots(
        self,
        screenshots: List[Dict],
        service: ServiceDefinition,
        problem_statement: str,
        design_system: str,
    ) -> Dict:
        """Spawn ``claude --print --add-dir <screenshots_dir>`` and
        ask it to read the PNGs + return evaluation JSON. Falls back
        to a low-confidence result on subprocess failure so the UX
        loop keeps progressing.
        """
        if not screenshots:
            return {
                "overall_score": 5,
                "summary": "no screenshots to evaluate",
                "issues": [],
            }

        # All screenshots live in one dir (parent class writes them
        # to ``<workspace>/screenshots/*.png``). Take the parent of
        # the first one — they're all siblings.
        first_path = Path(screenshots[0]["path"])
        screenshots_dir = first_path.parent

        text_prompt = EVALUATE_PROMPT.format(
            app_description=problem_statement,
            framework=service.framework,
            design_system=design_system,
        )
        full_prompt = _eval_instructions(
            schema_str=json.dumps(EVALUATE_SCHEMA["schema"], indent=2),
        ) + "\n\n" + (
            f"APP CONTEXT:\n"
            f"  description: {problem_statement[:400]}\n"
            f"  framework: {service.framework}\n"
            f"  design system: {design_system}\n\n"
            f"Read every PNG in the directory you have access to, "
            f"then emit the JSON."
        )

        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(_ALLOWED_TOOLS),
            "--add-dir", str(screenshots_dir),
        ] + self._additional_args

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                cwd=str(screenshots_dir),
            )
        except subprocess.TimeoutExpired:
            _log(
                self._on_status,
                f"ClaudeUXDesigner: vision eval timed out after "
                f"{self._timeout_s:.0f}s"
            )
            return self._low_confidence_result("eval timeout")
        except FileNotFoundError as e:
            _log(
                self._on_status,
                f"ClaudeUXDesigner: claude binary missing: {e}"
            )
            return self._low_confidence_result(f"binary missing: {e}")

        elapsed = time.time() - t0
        _log(
            self._on_status,
            f"ClaudeUXDesigner: vision eval done in {elapsed:.1f}s "
            f"(exit {proc.returncode})"
        )

        if proc.returncode != 0:
            return self._low_confidence_result(
                f"exit {proc.returncode}: {(proc.stderr or '')[:200]}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return self._low_confidence_result(
                f"non-JSON CLI output: {proc.stdout[:200]}"
            )
        if payload.get("is_error"):
            return self._low_confidence_result(
                f"is_error=true: {(payload.get('result') or '')[:200]}"
            )

        result_text = payload.get("result") or ""
        parsed = self._parse_eval_json(result_text)
        if parsed is None:
            return self._low_confidence_result(
                f"unparseable JSON in result: {result_text[:200]}"
            )
        return parsed

    # ── Parse helpers ──────────────────────────────────────────────────

    @staticmethod
    def _parse_eval_json(text: str) -> Optional[Dict]:
        if not text:
            return None
        candidate = text.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        # Fenced JSON.
        for m in re.finditer(
            r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL,
        ):
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
        # ── Forward scan from the FIRST opening brace ──────────────────
        # Picks up the top-level object even when there's prose before
        # it. This is the typical Claude shape: "Here's the spec:\n\n
        # {...}\n". Critically, this is preferred over the
        # trailing-balanced scan: that one can mis-anchor on an inner
        # object when the top-level object is truncated/malformed at
        # the end (Claude run that motivated this fix: trailing ``;``
        # after the summary string + missing closing ``}``).
        first_open = text.find("{")
        if first_open != -1:
            candidate_block = ClaudeUXDesigner._scan_forward_balanced(
                text, first_open,
            )
            if candidate_block is None:
                # Truncated — use everything from first_open to end and
                # let repair try to close it.
                candidate_block = text[first_open:]
            try:
                return json.loads(candidate_block)
            except json.JSONDecodeError:
                repaired = ClaudeUXDesigner._best_effort_repair(
                    candidate_block,
                )
                if repaired is not None:
                    return repaired
        # ── Trailing balanced fallback ────────────────────────────────
        attempts = 0
        scan_end = len(text)
        while attempts < 5:
            attempts += 1
            depth = 0
            end = None
            start_idx = None
            for i in range(scan_end - 1, -1, -1):
                c = text[i]
                if c == "}":
                    if end is None:
                        end = i
                    depth += 1
                elif c == "{":
                    depth -= 1
                    if depth == 0 and end is not None:
                        start_idx = i
                        break
            if start_idx is None or end is None:
                break
            try:
                return json.loads(text[start_idx:end + 1])
            except json.JSONDecodeError:
                scan_end = start_idx
                continue
        return None

    @staticmethod
    def _scan_forward_balanced(text: str, start: int) -> Optional[str]:
        """Return ``text[start:end+1]`` for the balanced ``{...}`` that
        opens at ``start``. Handles quoted strings + escapes so braces
        inside string literals don't confuse the depth counter. Returns
        None if no balanced match found (truncation)."""
        depth = 0
        i = start
        in_string = False
        escape = False
        while i < len(text):
            c = text[i]
            if escape:
                escape = False
            elif c == "\\" and in_string:
                escape = True
            elif c == '"':
                in_string = not in_string
            elif not in_string:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]
            i += 1
        return None  # truncated — no balanced close

    @staticmethod
    def _best_effort_repair(block: str) -> Optional[Dict]:
        """Try a few small repairs on a near-miss JSON block.
        Currently:
          - strip trailing ``;`` (Claude sometimes appends one)
          - append a single ``}`` if the block is one-bracket short
          - both together
        Returns the parsed dict if any repair works, else None."""
        cleaned = block.rstrip()
        candidates = [cleaned]
        if cleaned.endswith(";"):
            stripped = cleaned[:-1].rstrip()
            candidates.append(stripped)
            candidates.append(stripped + "}")
        # Heuristic: if the block has one more ``{`` than ``}``,
        # appending a single ``}`` likely closes it.
        opens = cleaned.count("{")
        closes = cleaned.count("}")
        if opens == closes + 1:
            candidates.append(cleaned + "}")
            if cleaned.endswith(";"):
                candidates.append(cleaned[:-1].rstrip() + "}")
        for c in candidates:
            try:
                return json.loads(c)
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _low_confidence_result(reason: str) -> Dict:
        return {
            "overall_score": 5,
            "summary": f"ClaudeUXDesigner eval failed: {reason}",
            "issues": [],
        }
