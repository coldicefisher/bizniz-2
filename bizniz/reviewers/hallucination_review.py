"""AI-driven hallucination review — the single guard.

Replaces the old hardcoded path-token vocab. Runs once at the
engineer's post-flight checkpoint, after all tickets passed and
files are in their final state.

The premise: "is this code hallucinated relative to the problem
statement" is a context-sensitive judgment a small LLM does well.
Hardcoded vocab can't (and never could). For greenfield projects
the AI sees the problem statement + the files; for vehinexa-style
existing codebases it also sees that the codebase already exists
and the question becomes "do these changes drift from the project's
established domain."

Cost shape: one LLM call per service per engineer pass. Cheap
model. Bounded prompt size (we cap file count + per-file lines).
Compare to the old approach: a path check on every debugger fix,
running 11 times across 2 tiers — same dollar cost ballpark, vastly
more accurate, no maintenance.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from bizniz.clients.base_ai_client import BaseAIClient


_REVIEWER_SYSTEM_PROMPT = """\
You are a code review agent. Your one job: identify HALLUCINATIONS
in code changes — where the engineer wrote code that introduces
domain concepts, feature areas, or business logic that have no
grounding in the problem statement.

Examples of hallucinations (FLAG these):
- Property-manager problem statement → file imports `grooming_service`
- Auth-only milestone → routes for `/payments/*` appear unprompted
- Inventory tracking project → schema for `appointment_booking`
- Real-estate app → references to `pet_clinic` or `vet_records`

Examples of NON-hallucinations (DO NOT FLAG):
- Files using framework conventions: `models/`, `routes/`, `schemas/`,
  `services/`, `tests/`, `app/`, `src/`, `core/`, `api/`, `db/`
- Test files mirroring source they exercise (test_property.py for property.py)
- Standard libraries / dependencies (sqlalchemy, pydantic, fastapi, jose,
  httpx, pytest, react, angular, etc.)
- Files extending existing legitimate patterns already in the codebase
- Auth scaffolding (login, logout, register, jwt, jwks, token, refresh,
  current_user, require_roles) — this is universal across SaaS
- Common SaaS plumbing: pagination, filtering, error handling, rate
  limiting, CORS, health checks
- Domain words that ARE in the problem statement, even if used in
  unexpected files

You are NOT reviewing code quality, performance, or correctness. ONLY
hallucinations: did the engineer invent a domain that has no source
in the problem statement.

Respond with ONLY a JSON object matching this schema:
{
  "clean": boolean,
  "summary": "one-line summary",
  "suspicious_files": [
    {
      "filepath": "string",
      "reason": "one-sentence explanation of what concept appears
                 to be invented and why",
      "severity": "blocker" | "warning"
    }
  ]
}

If clean (no hallucinations), suspicious_files MUST be an empty array.
Use "blocker" for clearly-out-of-scope domains. Use "warning" for
borderline cases where the file might be tangential but not obviously
wrong. When in doubt, prefer "clean: true" — false positives are
worse than false negatives, because they break legitimate work.
"""


@dataclass
class SuspiciousFile:
    filepath: str
    reason: str
    severity: str  # "blocker" | "warning"


@dataclass
class HallucinationReport:
    clean: bool
    summary: str = ""
    suspicious_files: List[SuspiciousFile] = field(default_factory=list)
    skipped_reason: Optional[str] = None  # set when we couldn't run

    @property
    def blockers(self) -> List[SuspiciousFile]:
        return [s for s in self.suspicious_files if s.severity == "blocker"]

    @property
    def has_blockers(self) -> bool:
        return any(s.severity == "blocker" for s in self.suspicious_files)


def _format_files_section(
    files_by_path: "dict[str, str]",
    max_files: int = 25,
    max_lines_per_file: int = 80,
) -> str:
    """Build the prompt section listing changed files. Caps both
    file count and per-file line count to keep the prompt bounded.
    """
    parts = []
    for i, (path, content) in enumerate(files_by_path.items()):
        if i >= max_files:
            parts.append(f"\n[... {len(files_by_path) - max_files} more file(s) omitted for brevity ...]")
            break
        lines = content.splitlines()
        snippet = "\n".join(lines[:max_lines_per_file])
        if len(lines) > max_lines_per_file:
            snippet += f"\n[... {len(lines) - max_lines_per_file} more lines ...]"
        parts.append(f"\n=== {path} ===\n{snippet}")
    return "\n".join(parts)


def review_for_hallucinations(
    *,
    problem_statement: str,
    changed_files: "dict[str, str]",
    ai_client: BaseAIClient,
    on_status: Optional[Callable[[str], None]] = None,
    max_files: int = 25,
    max_lines_per_file: int = 80,
) -> HallucinationReport:
    """Run a focused LLM review of the engineer's output.

    ``changed_files`` is a mapping of relative path → file content.
    The reviewer sees the problem statement + the files and emits a
    structured report. Caller decides what to do with blockers (we
    recommend: fail the service, log the report, abort milestone).

    Soft-fails to ``clean=True`` with ``skipped_reason`` when the AI
    call errors — the rest of the pipeline (post-flight type-check,
    integration tests) will still catch real bugs. We don't want a
    flaky reviewer to fail an otherwise-good engineering pass.
    """
    if not changed_files:
        return HallucinationReport(
            clean=True,
            summary="no files to review",
            skipped_reason="empty_changed_files",
        )

    prompt = (
        f"PROBLEM STATEMENT:\n{problem_statement.strip()}\n\n"
        f"CHANGED FILES (engineer just produced these):\n"
        f"{_format_files_section(changed_files, max_files, max_lines_per_file)}\n\n"
        f"Respond with the JSON object."
    )

    if on_status:
        on_status(
            f"Hallucination review: {len(changed_files)} file(s) "
            f"({sum(len(c) for c in changed_files.values())} chars)"
        )

    try:
        from bizniz.clients.chatgpt.messages import Message
        text, _job_id, _msgs = ai_client.get_text(
            messages=[
                Message(role="system", content=_REVIEWER_SYSTEM_PROMPT),
                Message(role="user", content=prompt),
            ],
            response_format=None,  # let the model emit JSON; we parse defensively
        )
    except Exception as e:
        if on_status:
            on_status(
                f"Hallucination review: AI call failed "
                f"({type(e).__name__}: {e}) — soft-passing"
            )
        return HallucinationReport(
            clean=True,
            summary=f"reviewer unavailable: {type(e).__name__}",
            skipped_reason=f"ai_call_failed:{type(e).__name__}",
        )

    return _parse_review_response(text, on_status=on_status)


def _parse_review_response(
    text: str,
    on_status: Optional[Callable[[str], None]] = None,
) -> HallucinationReport:
    """Tolerant JSON extraction: strips fences, finds first {...}
    block. AI sometimes wraps in markdown despite the prompt."""
    raw = text.strip()
    if raw.startswith("```"):
        # fenced — drop opening fence (with optional language tag) and
        # closing fence
        first_nl = raw.find("\n")
        if first_nl != -1:
            raw = raw[first_nl + 1 :]
        if raw.endswith("```"):
            raw = raw[: -3]
        raw = raw.strip()

    # Find first {...} block in case the model added prose around it
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        if on_status:
            on_status(
                "Hallucination review: response had no JSON object — "
                "soft-passing"
            )
        return HallucinationReport(
            clean=True,
            summary="reviewer response unparseable",
            skipped_reason="no_json_in_response",
        )

    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as e:
        if on_status:
            on_status(
                f"Hallucination review: JSON parse failed ({e}) — "
                f"soft-passing"
            )
        return HallucinationReport(
            clean=True,
            summary=f"reviewer response invalid JSON: {e}",
            skipped_reason="json_parse_failed",
        )

    suspicious = [
        SuspiciousFile(
            filepath=str(s.get("filepath", "")),
            reason=str(s.get("reason", "")),
            severity=("blocker" if s.get("severity") == "blocker" else "warning"),
        )
        for s in (data.get("suspicious_files") or [])
        if s.get("filepath")
    ]
    return HallucinationReport(
        clean=bool(data.get("clean", True)),
        summary=str(data.get("summary", "") or ""),
        suspicious_files=suspicious,
    )


def collect_changed_files(
    workspace_root: Path,
    *,
    extensions: tuple = (".py", ".ts", ".tsx", ".js", ".jsx"),
    max_files: int = 50,
    skip_dirs: tuple = (
        "__pycache__", ".git", "node_modules", "dist", "build",
        ".venv", "venv", ".pytest_cache", ".next",
    ),
) -> "dict[str, str]":
    """Helper: read every source file under ``workspace_root``, return
    {relative_path: contents}. Used by the architect to build the
    review's input. Bounded by ``max_files`` and the directory skip
    list so we don't read megabytes of generated code.
    """
    out: "dict[str, str]" = {}
    workspace_root = Path(workspace_root)
    for path in workspace_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in extensions:
            continue
        if any(seg in skip_dirs for seg in path.parts):
            continue
        try:
            rel = str(path.relative_to(workspace_root))
        except ValueError:
            continue
        try:
            out[rel] = path.read_text(errors="replace")
        except Exception:
            continue
        if len(out) >= max_files:
            break
    return out
