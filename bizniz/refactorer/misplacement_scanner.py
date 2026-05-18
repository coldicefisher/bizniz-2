"""Misplaced-business-logic scanner (D19 / v3 refactorer Signal 2).

The deterministic scanners (anti_patterns.py + cpd.py) find code
that's WRONG (bad patterns) or DUPLICATED across files. They miss
the third class of refactor target:

    Code that's correct + unique but in the wrong LAYER.

Example: a FastAPI route handler with 50 lines that do tax
calculation, business validation, and DB orchestration. The code
isn't wrong, isn't duplicated yet — but it doesn't belong in a
route handler. It should be a service-layer function in
``core/python/`` that the route thin-wraps.

A deterministic scanner can't reliably catch this. "Looks like
business logic" requires semantic judgment. The agent reads the
file and decides.

Scope (v3, Python-only):
  - Walks every Python file under known "frontline" directories
    (API routes, worker tasks, CLI entry points)
  - Per file, one LLM call returns a list of candidates as JSON
  - Each candidate names function/lines + suggests a core module

Frontline path patterns (matchable by suffix glob):

  - ``app/api/routes/`` — FastAPI routes
  - ``app/workers/`` — Celery / arq worker tasks
  - ``app/cli/`` — Click/Typer command entry points
  - ``app/main.py`` is excluded — startup wiring, not business logic

Output flows into the same per-candidate pipeline as CPD findings:
decision gate → planner → executor → verify.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from pydantic import BaseModel, Field


# Glob patterns relative to project root that ID "frontline" code.
# A file at ``backend/app/api/routes/recipes.py`` matches
# ``*/app/api/routes/*.py`` — service-name agnostic.
DEFAULT_FRONTLINE_GLOBS: Tuple[str, ...] = (
    "*/app/api/routes/*.py",
    "*/app/workers/*.py",
    "*/app/cli/*.py",
)

# Files within frontline directories that are NEVER candidates.
# Avoid scanning __init__.py, conftest.py, etc.
SKIP_FILENAMES: Tuple[str, ...] = ("__init__.py", "conftest.py")


class MisplacedLogicCandidate(BaseModel):
    """One agent-flagged candidate.

    Shape matches what flows downstream into the decision gate's
    ``CandidateContext``."""
    file_path: str = Field(..., description="Source file the candidate lives in.")
    function_name: str = Field(
        ...,
        description="Name of the function/method holding the misplaced code.",
    )
    line_range: Tuple[int, int] = Field(
        ...,
        description="(start, end) 1-based inclusive line numbers.",
    )
    why: str = Field(
        ...,
        description=(
            "Agent's explanation — why this is misplaced, not just "
            "'business-y-looking'."
        ),
    )
    suggested_core_module: str = Field(
        ...,
        description=(
            "Agent's proposed destination, e.g. "
            "'core/python/recipes/validation.py'. Planner refines."
        ),
    )


class MisplacementReport(BaseModel):
    """End-of-scan summary."""
    candidates: List[MisplacedLogicCandidate] = Field(default_factory=list)
    files_scanned: int = 0
    files_skipped: int = 0
    skipped_reasons: List[str] = Field(default_factory=list)


# Schema for response_format-aware clients.
SCAN_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "function_name", "line_start", "line_end",
                    "why", "suggested_core_module",
                ],
                "properties": {
                    "function_name": {"type": "string"},
                    "line_start": {"type": "integer", "minimum": 1},
                    "line_end": {"type": "integer", "minimum": 1},
                    "why": {"type": "string", "minLength": 1},
                    "suggested_core_module": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "maxItems": 5,
        },
    },
    "additionalProperties": False,
}


_SYSTEM_PROMPT = """\
You scan Python files in frontline layers (API routes, worker
tasks, CLI commands) for code that DOESN'T BELONG there.

What belongs in a frontline file:
  - Request/job parsing + validation that's tied to the protocol
    (Pydantic models for HTTP, message schemas for queues)
  - Auth checks via framework dependencies
  - Calling INTO domain code
  - Formatting responses

What DOESN'T belong here (misplaced):
  - Business rules (tax computation, eligibility logic, scoring)
  - Data transformations beyond protocol concerns
  - Multi-step orchestration that doesn't depend on the HTTP/queue
    layer
  - Validation that goes beyond Pydantic / framework-built-in
  - Side-effect coordination (sending emails, calling other APIs)
    that isn't request-shape-bound

What's idiomatic and should NOT be flagged:
  - Calling a domain function with parsed inputs
  - Short helper functions that exist only because the route uses
    them once
  - Framework boilerplate (Depends, Body, response_model, etc.)
  - Error → HTTP status translation

Return at most 5 candidates per file. If everything in the file is
idiomatic frontline code, return an empty list — that's the right
answer most of the time.

Format your response as VALID JSON matching this schema:

  {
    "candidates": [
      {
        "function_name": "create_recipe",
        "line_start": 42,
        "line_end": 67,
        "why": "computes tax and applies pricing rules — both
               domain concerns; should be a service function",
        "suggested_core_module": "core/python/recipes/pricing.py"
      }
    ]
  }

If the file is short / contains no misplaced logic:

  { "candidates": [] }

Be conservative. False positives are worse than missing one. The
downstream decision gate has the final say; you're a coarse
filter.
"""


_USER_TEMPLATE = """\
File: {file_path}

```python
{content}
```

Identify misplaced business logic per your system prompt. Return
JSON.
"""


class MisplacementScanner:
    """Walks frontline files, dispatches per-file LLM scans.

    The LLM call shape is up to the caller: pass an ``llm_invoker``
    that takes ``(system_prompt, user_prompt)`` and returns text.
    The scanner parses the returned JSON, never raises on bad
    responses — bad responses just produce zero candidates for
    that file.
    """

    def __init__(
        self,
        project_root: Path,
        llm_invoker: Callable[[str, str], str],
        frontline_globs: Tuple[str, ...] = DEFAULT_FRONTLINE_GLOBS,
        max_file_chars: int = 12000,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._invoke = llm_invoker
        self._globs = tuple(frontline_globs)
        self._max_file_chars = max_file_chars
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    # ── Public ─────────────────────────────────────────────────────

    def scan(self) -> MisplacementReport:
        """Walk + scan; return aggregated report. Never raises."""
        report = MisplacementReport()
        frontline_files = self._discover_frontline_files()
        self._log(
            f"MisplacementScanner: {len(frontline_files)} frontline "
            f"file(s) to scan"
        )
        for path in frontline_files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as e:
                report.files_skipped += 1
                report.skipped_reasons.append(
                    f"{path}: read failed ({e})"
                )
                continue
            if not text.strip():
                report.files_skipped += 1
                continue
            report.files_scanned += 1
            candidates = self._scan_one(path, text)
            report.candidates.extend(candidates)
        self._log(
            f"MisplacementScanner: scan complete — "
            f"{report.files_scanned} scanned, "
            f"{report.files_skipped} skipped, "
            f"{len(report.candidates)} candidate(s)"
        )
        return report

    # ── Internals ──────────────────────────────────────────────────

    def _discover_frontline_files(self) -> List[Path]:
        """Walk project root looking for files matching any of the
        frontline globs. Skips ``__init__.py`` and ``conftest.py``.

        Skips anything under ``.bizniz/`` (state) or ``tests/`` —
        we only refactor production code."""
        out: List[Path] = []
        for path in self._project_root.rglob("*.py"):
            if path.name in SKIP_FILENAMES:
                continue
            try:
                rel = path.relative_to(self._project_root)
            except ValueError:
                continue
            rel_str = str(rel).replace("\\", "/")
            if rel_str.startswith(".bizniz/"):
                continue
            # Test files live under tests/ or have test_* prefix.
            if "/tests/" in rel_str or rel.name.startswith("test_"):
                continue
            if any(fnmatch.fnmatch(rel_str, g) for g in self._globs):
                out.append(path)
        return sorted(out)

    def _scan_one(
        self, path: Path, content: str,
    ) -> List[MisplacedLogicCandidate]:
        """Dispatch one LLM call against one file's content. Parse
        the response into candidates. Never raises."""
        # Truncate huge files; the LLM doesn't need the whole thing
        # to find misplaced logic.
        snippet = (
            content if len(content) <= self._max_file_chars
            else content[: self._max_file_chars] + "\n# ... (truncated)\n"
        )
        try:
            rel = str(path.relative_to(self._project_root))
        except ValueError:
            rel = str(path)
        user_prompt = _USER_TEMPLATE.format(
            file_path=rel, content=snippet,
        )
        try:
            raw = self._invoke(_SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            self._log(
                f"MisplacementScanner: scan of {rel} failed "
                f"({type(e).__name__}: {e}) — skipping file"
            )
            return []
        return _parse_response(raw, file_path=rel)


# ── Parsing ──────────────────────────────────────────────────────


def _parse_response(
    raw_text: str,
    file_path: str,
) -> List[MisplacedLogicCandidate]:
    """Pull the candidates array from the LLM response. Defensive
    — every shape error becomes "zero candidates for this file."""
    text = (raw_text or "").strip()
    if not text:
        return []
    # Strip code fences.
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(text[start: end + 1])
        except json.JSONDecodeError:
            return []
    if not isinstance(data, dict):
        return []
    raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list):
        return []

    out: List[MisplacedLogicCandidate] = []
    for entry in raw_candidates:
        if not isinstance(entry, dict):
            continue
        try:
            function_name = str(entry["function_name"])
            line_start = int(entry["line_start"])
            line_end = int(entry["line_end"])
            why = str(entry["why"])
            suggested = str(entry["suggested_core_module"])
        except (KeyError, TypeError, ValueError):
            continue
        if line_end < line_start:
            line_start, line_end = line_end, line_start
        out.append(MisplacedLogicCandidate(
            file_path=file_path,
            function_name=function_name,
            line_range=(line_start, line_end),
            why=why[:1000],
            suggested_core_module=suggested[:200],
        ))
    return out
