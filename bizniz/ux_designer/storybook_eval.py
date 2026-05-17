"""Per-story vision evaluation for the Storybook UX loop — Phase 3.

Takes a captured story screenshot (Phase 2) + story metadata
(Phase 1) and asks a vision model to score the primitive against
the project's established design system.

The mental frame is different from per-route eval:

  Per-route eval: "Is this dashboard page well-laid-out?"
  Per-story eval: "Is this Button primitive consistent with the
                   design system, accessible, and well-spaced?"

The primitive is evaluated in isolation — Storybook serves each
story in an iframe with no app chrome — so the vision model
focuses on the component itself, not the surrounding context.

This module is the orchestration boundary. The vision call is
injectable so tests can pass a fake. Production uses Claude CLI
subprocess (same pattern as ``pro_ux_designer._invoke_and_parse``).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Literal, Optional

from pydantic import BaseModel, Field

from bizniz.ux_designer.storybook_capture import StoryCaptureResult
from bizniz.ux_designer.storybook_discovery import StoryEntry


# ── Output schema ────────────────────────────────────────────────


class StoryEvalIssue(BaseModel):
    """One concrete issue the vision model spotted on this story."""
    severity: Literal["critical", "major", "minor"] = "minor"
    description: str
    suggested_fix: Optional[str] = None


class StoryEvalResult(BaseModel):
    """Outcome of evaluating one story."""
    story_id: str
    name: str
    title: str
    overall_score: int = Field(default=0, ge=0, le=10)
    matches_design_system: bool = False
    issues: List[StoryEvalIssue] = Field(default_factory=list)
    summary: str = ""
    stop_recommendation: Literal["stop", "iterate"] = "stop"


# ── Prompts ──────────────────────────────────────────────────────


_EVAL_SYSTEM_PROMPT = """You are a senior product designer evaluating a UI primitive (a Storybook story) for visual quality, design-system consistency, and accessibility.

You see ONE screenshot of a primitive component rendered in isolation (Storybook serves stories in an iframe with no app chrome). The primitive may be a button, card, modal, toast, form input, or any other reusable UI piece.

Score the component 0-10 on these dimensions, weighted equally:

1. **Design-system consistency** — Does it use the established color tokens, spacing scale, typography, and corner radii? Does it look like it belongs to the same product as the other primitives?
2. **Visual polish** — Are spacing, alignment, typography, and color combinations actually pleasing? Or does it look raw / unstyled / placeholder-y?
3. **Affordance & state clarity** — If it's interactive, can a user tell what it does and what state it's in? (For non-interactive primitives like toasts/badges, is the message hierarchy clear?)
4. **Accessibility hints** — Sufficient contrast, readable text size, focus states visible (if applicable)?

Output STRICT JSON matching this shape (no commentary outside the JSON):

```json
{
  "overall_score": 0-10,
  "matches_design_system": true|false,
  "issues": [
    {"severity": "critical|major|minor", "description": "...", "suggested_fix": "..."}
  ],
  "summary": "one-sentence summary of the primitive's quality",
  "stop_recommendation": "stop|iterate"
}
```

`stop_recommendation` rules:
- "stop" if overall_score >= 7 OR issues list is empty
- "iterate" otherwise

`severity` rules:
- "critical" = breaks design-system consistency badly (wrong palette, plain-HTML look, missing all styling)
- "major" = noticeable visual or accessibility problem the user would spot
- "minor" = nitpick refinement
"""


_EVAL_USER_TEMPLATE = """Evaluate the Storybook story below.

**Story:**
- Title: {title}
- State: {name}
- Story id: {story_id}
- Component: {component_name}

{design_lock_section}

{iteration_section}

The screenshot of this story is in the directory you can read. Look at it, then output the JSON eval per the system prompt.
"""


def _build_user_prompt(
    entry: StoryEntry,
    iteration: int,
    design_lock_json: Optional[str],
) -> str:
    design_lock_section = ""
    if design_lock_json:
        design_lock_section = (
            "**Established design system (the primitive should match this):**\n"
            f"```json\n{design_lock_json}\n```\n"
        )
    iteration_section = (
        f"**Iteration:** {iteration}"
        if iteration > 1
        else ""
    )
    return _EVAL_USER_TEMPLATE.format(
        title=entry.title,
        name=entry.name,
        story_id=entry.story_id,
        component_name=entry.component_name or "(unknown)",
        design_lock_section=design_lock_section,
        iteration_section=iteration_section,
    )


# ── Result parsing ───────────────────────────────────────────────


def _parse_eval_json(raw: dict, entry: StoryEntry) -> StoryEvalResult:
    """Coerce raw vision JSON into a ``StoryEvalResult``. Tolerates
    missing/garbage fields by defaulting to conservative values."""
    score = raw.get("overall_score")
    try:
        score_i = int(score) if score is not None else 0
    except (TypeError, ValueError):
        score_i = 0
    score_i = max(0, min(10, score_i))

    issues_raw = raw.get("issues") or []
    issues: List[StoryEvalIssue] = []
    if isinstance(issues_raw, list):
        for it in issues_raw:
            if not isinstance(it, dict):
                continue
            try:
                issues.append(StoryEvalIssue.model_validate(it))
            except Exception:
                # Best-effort — skip malformed issue dicts.
                continue

    stop_rec = raw.get("stop_recommendation", "stop")
    if stop_rec not in ("stop", "iterate"):
        stop_rec = "stop"

    return StoryEvalResult(
        story_id=entry.story_id,
        name=entry.name,
        title=entry.title,
        overall_score=score_i,
        matches_design_system=bool(raw.get("matches_design_system", False)),
        issues=issues,
        summary=str(raw.get("summary") or ""),
        stop_recommendation=stop_rec,
    )


# ── Evaluator ────────────────────────────────────────────────────


class StoryEvaluator:
    """Drives per-story vision evaluation.

    The ``vision_invoker`` is injectable. Default uses Claude CLI
    subprocess; tests can pass a fake that returns canned JSON.
    """

    def __init__(
        self,
        command: str = "claude",
        on_status: Optional[Callable[[str], None]] = None,
        vision_invoker: Optional[Callable[[StoryEntry, str, Path], Optional[dict]]] = None,
        additional_args: Optional[List[str]] = None,
    ) -> None:
        self._command = command
        self._on_status = on_status
        self._vision_invoker = vision_invoker or self._default_vision_invoker
        self._additional_args = list(additional_args or [])

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def evaluate(
        self,
        capture: StoryCaptureResult,
        entry: StoryEntry,
        design_lock_json: Optional[str] = None,
        iteration: int = 1,
    ) -> StoryEvalResult:
        """Evaluate one captured story. Returns a result even on
        failure — never raises. Failed eval gets ``overall_score=0``
        and ``stop_recommendation='stop'``."""
        if not capture.success or capture.screenshot_path is None:
            self._log(
                f"StoryEvaluator: {entry.story_id} skipped — no usable capture"
            )
            return StoryEvalResult(
                story_id=entry.story_id, name=entry.name, title=entry.title,
                overall_score=0,
                summary="no capture available",
                stop_recommendation="stop",
            )
        screenshot_dir = capture.screenshot_path.parent
        user_prompt = _build_user_prompt(entry, iteration, design_lock_json)
        self._log(
            f"StoryEvaluator: evaluating {entry.story_id} "
            f"(iter {iteration})..."
        )
        parsed = self._vision_invoker(entry, user_prompt, screenshot_dir)
        if parsed is None:
            self._log(
                f"StoryEvaluator: {entry.story_id} vision returned no "
                f"parseable JSON — marking score 0"
            )
            return StoryEvalResult(
                story_id=entry.story_id, name=entry.name, title=entry.title,
                overall_score=0,
                summary="vision call returned no parseable JSON",
                stop_recommendation="stop",
            )
        result = _parse_eval_json(parsed, entry)
        self._log(
            f"StoryEvaluator: {entry.story_id} score={result.overall_score}/10 "
            f"({len(result.issues)} issue(s), {result.stop_recommendation})"
        )
        return result

    # ── Default Claude CLI invoker ───────────────────────────────

    def _default_vision_invoker(
        self,
        entry: StoryEntry,
        user_prompt: str,
        screenshot_dir: Path,
    ) -> Optional[dict]:
        if shutil.which(self._command) is None:
            self._log(
                f"StoryEvaluator: {self._command!r} not on PATH — "
                f"cannot run vision eval"
            )
            return None
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", _EVAL_SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--add-dir", str(screenshot_dir),
        ] + self._additional_args
        try:
            proc = subprocess.run(
                cmd, input=user_prompt, capture_output=True,
                text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            self._log(
                f"StoryEvaluator: {entry.story_id} vision call timed out"
            )
            return None
        if proc.returncode != 0:
            self._log(
                f"StoryEvaluator: {entry.story_id} vision exit "
                f"{proc.returncode}; stderr tail: {proc.stderr[-200:]}"
            )
            return None
        # Claude CLI returns its own JSON envelope; the model's reply
        # is in result["result"]. Parse twice — outer envelope first,
        # inner reply second (the model is asked to emit JSON).
        try:
            envelope = json.loads(proc.stdout)
        except Exception:
            return None
        inner = envelope.get("result")
        if not isinstance(inner, str):
            return None
        # The model's reply may have prose before/after the JSON;
        # extract the first balanced { ... } block.
        start = inner.find("{")
        end = inner.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(inner[start:end + 1])
        except Exception:
            return None
