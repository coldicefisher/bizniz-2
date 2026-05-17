"""Storybook screenshot capture for ProUXDesigner — Phase 2b.

Given a ``StoryCatalog`` (Phase 1) and a running Storybook base
URL (Phase 2a), drives a Playwright sidecar to screenshot each
story and returns one ``StoryCaptureResult`` per entry.

The sidecar JS lives at
``bizniz/ux_designer/sidecars/storybook_capture.cjs``. It reads a
JSON plan from stdin, navigates to each story's iframe URL, and
writes PNGs into the output directory. We invoke it via a
Node-capable container (the Playwright sidecar image already
used by UXDesigner for route capture).

This module is the orchestration boundary — the sidecar
invocation is parameterized so tests can pass a fake.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.ux_designer.storybook_discovery import StoryCatalog, StoryEntry


# Default capture timeout per sidecar invocation. The sidecar
# screenshots many stories in one Node process, so the budget is
# bigger than route-capture (which fans out per-route in parallel).
_DEFAULT_CAPTURE_TIMEOUT_S = 300.0

# Where the sidecar JS lives — relative to this file.
_SIDECAR_JS = Path(__file__).resolve().parent / "sidecars" / "storybook_capture.cjs"


class StoryCaptureResult(BaseModel):
    """Outcome of capturing one story."""
    story_id: str
    name: str
    title: str
    screenshot_path: Optional[Path] = Field(
        default=None,
        description="Absolute path to the PNG on success; None on failure.",
    )
    success: bool = False
    error: Optional[str] = Field(
        default=None,
        description="Short error message from the sidecar if capture failed.",
    )


class CapturePlan(BaseModel):
    """Plan we hand to the sidecar JS via stdin JSON."""
    storybook_base_url: str
    output_dir: str
    viewport_width: int = 1280
    viewport_height: int = 720
    wait_after_load_ms: int = 600  # Settle animations/transitions.
    stories: List[dict] = Field(default_factory=list)


def _story_url(base_url: str, story_id: str) -> str:
    """Storybook serves each story at /iframe.html?id=<id>&viewMode=story."""
    return f"{base_url.rstrip('/')}/iframe.html?id={story_id}&viewMode=story"


def build_capture_plan(
    catalog: StoryCatalog,
    storybook_base_url: str,
    output_dir: Path,
    viewport_width: int = 1280,
    viewport_height: int = 720,
) -> CapturePlan:
    """Build the JSON plan handed to the sidecar.

    Each story gets one entry: its URL + the absolute path to write
    its PNG. Output filenames are ``<story_id>.png`` (story_ids are
    kebab-case already, safe for filenames).
    """
    output_dir = Path(output_dir).resolve()
    stories: List[dict] = []
    for entry in catalog.stories:
        stories.append({
            "story_id": entry.story_id,
            "name": entry.name,
            "title": entry.title,
            "url": _story_url(storybook_base_url, entry.story_id),
            "output_path": str(output_dir / f"{entry.story_id}.png"),
        })
    return CapturePlan(
        storybook_base_url=storybook_base_url,
        output_dir=str(output_dir),
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        stories=stories,
    )


def capture_stories(
    catalog: StoryCatalog,
    storybook_base_url: str,
    output_dir: Path,
    timeout_s: float = _DEFAULT_CAPTURE_TIMEOUT_S,
    on_status: Optional[Callable[[str], None]] = None,
    # Injected for tests.
    sidecar_invoker: Optional[Callable[[CapturePlan, float], "SidecarResult"]] = None,
) -> List[StoryCaptureResult]:
    """Capture every story in ``catalog``. Returns one result per
    entry, in catalog order. Always returns one result per story —
    failures get ``success=False`` + an error message rather than
    raising.
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    plan = build_capture_plan(catalog, storybook_base_url, output_dir)
    if on_status:
        on_status(
            f"StorybookCapture: planning {len(plan.stories)} story shot(s) "
            f"against {storybook_base_url}"
        )
    if not plan.stories:
        return []

    invoke = sidecar_invoker or _default_sidecar_invoker
    sidecar_result = invoke(plan, timeout_s)

    # Parse sidecar's per-story status. Sidecar emits a JSON line
    # per story to stdout: ``{"story_id": ..., "success": ..., ...}``.
    by_story: dict = {}
    for line in sidecar_result.stdout_lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        sid = obj.get("story_id")
        if sid is not None:
            by_story[sid] = obj

    results: List[StoryCaptureResult] = []
    for entry in catalog.stories:
        record = by_story.get(entry.story_id)
        if record is None:
            # The sidecar didn't report on this story — treat as a
            # failure with whatever stderr we have.
            results.append(StoryCaptureResult(
                story_id=entry.story_id,
                name=entry.name,
                title=entry.title,
                screenshot_path=None,
                success=False,
                error="sidecar did not report on this story",
            ))
            continue
        success = bool(record.get("success"))
        path_str = record.get("output_path") or ""
        screenshot_path = Path(path_str) if path_str else None
        if success and screenshot_path is not None and not screenshot_path.is_file():
            # Sidecar said success but the file isn't there → mark
            # failed; the file is the source of truth.
            success = False
            error = f"sidecar reported success but PNG missing at {screenshot_path}"
            screenshot_path = None
        else:
            error = record.get("error")
        results.append(StoryCaptureResult(
            story_id=entry.story_id,
            name=entry.name,
            title=entry.title,
            screenshot_path=screenshot_path,
            success=success,
            error=error,
        ))

    if on_status:
        ok = sum(1 for r in results if r.success)
        on_status(
            f"StorybookCapture: {ok}/{len(results)} story shot(s) captured"
        )
    return results


# ── Sidecar invocation ───────────────────────────────────────────


class SidecarResult(BaseModel):
    """Raw result of running the sidecar — stdout + stderr lines +
    exit code. Captured separately from screenshot results so tests
    can fake either side independently."""
    exit_code: int = 0
    stdout_lines: List[str] = Field(default_factory=list)
    stderr_lines: List[str] = Field(default_factory=list)


def _default_sidecar_invoker(plan: CapturePlan, timeout_s: float) -> SidecarResult:
    """Production sidecar invocation: run the .cjs script via node.

    The script reads the JSON plan from stdin, writes per-story
    status lines to stdout, and PNGs to disk. Stderr carries
    Playwright noise.
    """
    if not _SIDECAR_JS.is_file():
        return SidecarResult(
            exit_code=127,
            stderr_lines=[f"storybook_capture.cjs missing at {_SIDECAR_JS}"],
        )
    try:
        proc = subprocess.run(
            ["node", str(_SIDECAR_JS)],
            input=plan.model_dump_json(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or "").splitlines() if isinstance(e.stdout, str) else []
        stderr = (e.stderr or "").splitlines() if isinstance(e.stderr, str) else []
        stderr.append(f"sidecar timed out after {timeout_s:.0f}s")
        return SidecarResult(exit_code=124, stdout_lines=stdout, stderr_lines=stderr)
    except FileNotFoundError as e:
        return SidecarResult(
            exit_code=127,
            stderr_lines=[f"node not on PATH: {e}"],
        )
    return SidecarResult(
        exit_code=proc.returncode,
        stdout_lines=(proc.stdout or "").splitlines(),
        stderr_lines=(proc.stderr or "").splitlines(),
    )
