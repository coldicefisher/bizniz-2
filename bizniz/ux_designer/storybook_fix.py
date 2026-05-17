"""Per-story fix dispatch for the Storybook UX loop — Phase 4.

Takes a ``StoryEvalResult`` (Phase 3) + a ``StoryEntry`` (Phase 1)
and dispatches Coder against the component file to address the
issues. Returns a ``StoryFixResult`` describing what changed.

Mental frame: per-route fix targets a page; per-story fix targets
a primitive. The component file is known (from Phase 1's import
resolution), so the prompt can point Coder directly at it rather
than asking it to determine the right file from context.

This module is the orchestration boundary. The Coder invocation
is injectable for tests; production uses Claude CLI subprocess.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.ux_designer.storybook_discovery import StoryEntry
from bizniz.ux_designer.storybook_eval import StoryEvalResult


# ── Output schema ────────────────────────────────────────────────


class StoryFixResult(BaseModel):
    """Outcome of attempting to fix one story."""
    story_id: str
    name: str
    title: str
    status: str = "failed"  # "applied" / "no_changes" / "failed"
    files_written: List[str] = Field(default_factory=list)
    summary: str = ""
    notes: List[str] = Field(default_factory=list)


# ── Prompts ──────────────────────────────────────────────────────


_FIX_SYSTEM_PROMPT = """You are a senior product designer + frontend engineer fixing a UI primitive (a Storybook story) to address specific design-quality issues.

You will be told:
- The primitive's name, state, and source file
- A list of concrete issues to fix (with severity)
- The established design system (color tokens, spacing scale, type)

Your job:
1. Read the component file at the path you're given
2. Read its sibling `.stories.tsx` file if helpful
3. Apply the smallest set of edits that resolves the listed issues
4. **Use the established design system tokens, not hard-coded values**
5. Do NOT change the component's public API (props, exports) unless an issue explicitly asks for it
6. Do NOT change the story file unless the issue is about the story itself (default props, missing state)

Output STRICT JSON when you finish, no commentary:

```json
{
  "status": "applied|no_changes|failed",
  "files_written": ["/abs/path/to/edited/file.tsx", ...],
  "summary": "one-sentence summary of what changed and why",
  "notes": ["optional supplementary observations"]
}
```

`status` rules:
- "applied" = at least one file was edited to address an issue
- "no_changes" = the existing code already matches the spec; no edits needed
- "failed" = couldn't fix (component file missing, edits would break the build, etc.)
"""


_FIX_USER_TEMPLATE = """Fix the Storybook primitive below.

**Primitive:**
- Title: {title}
- State: {name}
- Story id: {story_id}
- Component name: {component_name}
- Component file: {component_file}

{design_lock_section}

**Issues to fix (newest first):**

{issues_block}

Read the component file, apply the smallest edit set that resolves these issues using the design-system tokens, then emit the JSON per the system prompt.
"""


def _format_issues(eval_result: StoryEvalResult) -> str:
    if not eval_result.issues:
        return "(no issues — should not be called)"
    lines: List[str] = []
    for i, issue in enumerate(eval_result.issues, 1):
        fix = issue.suggested_fix or "(determine the fix from context)"
        lines.append(
            f"{i}. [{issue.severity.upper()}] {issue.description}\n"
            f"   Suggested fix: {fix}"
        )
    return "\n\n".join(lines)


def _build_user_prompt(
    entry: StoryEntry,
    eval_result: StoryEvalResult,
    design_lock_json: Optional[str],
) -> str:
    design_lock_section = ""
    if design_lock_json:
        design_lock_section = (
            "**Established design system (use these tokens, not "
            "hard-coded values):**\n"
            f"```json\n{design_lock_json}\n```\n"
        )
    component_file = (
        str(entry.component_file)
        if entry.component_file is not None
        else "(unknown — read the stories file at "
             f"{entry.stories_file} and locate the import)"
    )
    return _FIX_USER_TEMPLATE.format(
        title=entry.title,
        name=entry.name,
        story_id=entry.story_id,
        component_name=entry.component_name or "(unknown)",
        component_file=component_file,
        design_lock_section=design_lock_section,
        issues_block=_format_issues(eval_result),
    )


# ── Result parsing ───────────────────────────────────────────────


def _parse_fix_json(raw: dict, entry: StoryEntry) -> StoryFixResult:
    status = raw.get("status", "failed")
    if status not in ("applied", "no_changes", "failed"):
        status = "failed"
    files = raw.get("files_written") or []
    if not isinstance(files, list):
        files = []
    notes = raw.get("notes") or []
    if not isinstance(notes, list):
        notes = []
    return StoryFixResult(
        story_id=entry.story_id,
        name=entry.name,
        title=entry.title,
        status=status,
        files_written=[str(f) for f in files if isinstance(f, str)],
        summary=str(raw.get("summary") or ""),
        notes=[str(n) for n in notes if isinstance(n, str)],
    )


# ── Dispatcher ───────────────────────────────────────────────────


class StoryFixDispatcher:
    """Drives per-story fix dispatch.

    The ``coder_invoker`` is injectable. Default uses Claude CLI
    subprocess with Edit/Write/Read/Bash/Glob/Grep tools against
    the frontend workspace; tests pass a fake that returns canned
    JSON.
    """

    def __init__(
        self,
        command: str = "claude",
        on_status: Optional[Callable[[str], None]] = None,
        coder_invoker: Optional[Callable[[StoryEntry, str, Path], Optional[dict]]] = None,
        additional_args: Optional[List[str]] = None,
    ) -> None:
        self._command = command
        self._on_status = on_status
        self._coder_invoker = coder_invoker or self._default_coder_invoker
        self._additional_args = list(additional_args or [])

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def dispatch(
        self,
        entry: StoryEntry,
        eval_result: StoryEvalResult,
        frontend_root: Path,
        design_lock_json: Optional[str] = None,
    ) -> StoryFixResult:
        """Apply fixes for one story. Returns a result even on
        failure — never raises."""
        if not eval_result.issues:
            self._log(
                f"StoryFixDispatcher: {entry.story_id} — no issues to fix"
            )
            return StoryFixResult(
                story_id=entry.story_id, name=entry.name, title=entry.title,
                status="no_changes",
                summary="no issues to fix",
            )
        user_prompt = _build_user_prompt(entry, eval_result, design_lock_json)
        self._log(
            f"StoryFixDispatcher: {entry.story_id} dispatching "
            f"({len(eval_result.issues)} issue(s))..."
        )
        parsed = self._coder_invoker(entry, user_prompt, frontend_root)
        if parsed is None:
            self._log(
                f"StoryFixDispatcher: {entry.story_id} returned no "
                f"parseable JSON"
            )
            return StoryFixResult(
                story_id=entry.story_id, name=entry.name, title=entry.title,
                status="failed",
                summary="coder returned no parseable JSON",
            )
        result = _parse_fix_json(parsed, entry)
        self._log(
            f"StoryFixDispatcher: {entry.story_id} {result.status} "
            f"({len(result.files_written)} file(s) written)"
        )
        return result

    # ── Default Claude CLI invoker ───────────────────────────────

    def _default_coder_invoker(
        self,
        entry: StoryEntry,
        user_prompt: str,
        frontend_root: Path,
    ) -> Optional[dict]:
        if shutil.which(self._command) is None:
            self._log(
                f"StoryFixDispatcher: {self._command!r} not on PATH — "
                f"cannot dispatch fixes"
            )
            return None
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", _FIX_SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Edit Write Read Bash Glob Grep",
            "--add-dir", str(frontend_root),
        ] + self._additional_args
        try:
            proc = subprocess.run(
                cmd, input=user_prompt, capture_output=True,
                text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            self._log(
                f"StoryFixDispatcher: {entry.story_id} coder call timed out"
            )
            return None
        if proc.returncode != 0:
            self._log(
                f"StoryFixDispatcher: {entry.story_id} coder exit "
                f"{proc.returncode}; stderr tail: {proc.stderr[-200:]}"
            )
            return None
        try:
            envelope = json.loads(proc.stdout)
        except Exception:
            return None
        inner = envelope.get("result")
        if not isinstance(inner, str):
            return None
        # Extract first balanced { ... } block (model may have prose).
        start = inner.find("{")
        end = inner.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(inner[start:end + 1])
        except Exception:
            return None
