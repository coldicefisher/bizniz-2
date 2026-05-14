"""Plan + global-design cache for ProUXDesigner (item #4).

The first two phases of every UX review (``code_review`` reading
the codebase to emit a design plan, then ``global_design`` applying
tokens + primitives) run unconditionally each invocation. On
recipe_box's v2.7 run they cost ~440s combined — even though
nothing about the project's structure had changed since the prior
review.

This module caches both outputs to ``<project>/.bizniz/ux_plan.json``
keyed by an input-mtime fingerprint of the relevant source files:

  - ``src/**/*.{ts,tsx,jsx,js,css}``
  - ``tailwind.config.{ts,js,cjs}``
  - ``postcss.config.{js,cjs}``
  - ``package.json``, ``package-lock.json``, ``pnpm-lock.yaml``,
    ``yarn.lock``
  - ``index.html``

On entry to ``review_frontend``, ProUXDesigner computes the current
max-mtime across this set. If the cache's recorded mtime is at
least as recent as the current one (i.e. nothing edited since), the
cached plan + global-design result are returned and the two heavy
phases are skipped.

Skipping global_design is safe IF its prior outputs (the
``files_written`` list) are still on disk with mtimes >= the
recorded write time. If any output file was deleted or
mtime-clobbered, the cache is invalidated and the phase re-runs.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


CACHE_FILENAME = "ux_plan.json"


# Files we watch for the input fingerprint. Glob patterns relative
# to the workspace root.
_INPUT_GLOBS = (
    "src/**/*.ts",
    "src/**/*.tsx",
    "src/**/*.jsx",
    "src/**/*.js",
    "src/**/*.css",
    "src/**/*.scss",
)
_INPUT_NAMED_FILES = (
    "tailwind.config.ts",
    "tailwind.config.js",
    "tailwind.config.cjs",
    "postcss.config.js",
    "postcss.config.cjs",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "index.html",
    "vite.config.ts",
    "vite.config.js",
)


def compute_input_mtime(workspace_root: Path) -> Optional[float]:
    """Walk the watched globs + named files and return the latest
    mtime. ``None`` if nothing matched."""
    best: Optional[float] = None

    def _record(fp: Path) -> None:
        nonlocal best
        try:
            m = fp.stat().st_mtime
        except OSError:
            return
        if best is None or m > best:
            best = m

    for pat in _INPUT_GLOBS:
        for fp in workspace_root.glob(pat):
            if fp.is_file():
                _record(fp)
    for rel in _INPUT_NAMED_FILES:
        fp = workspace_root / rel
        if fp.is_file():
            _record(fp)
    return best


def cache_path(workspace_root: Path) -> Path:
    return workspace_root / ".bizniz" / CACHE_FILENAME


def load_cache(workspace_root: Path) -> Optional[Dict]:
    fp = cache_path(workspace_root)
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text())
    except Exception:
        return None


def save_cache(
    workspace_root: Path,
    *,
    plan: Dict,
    global_fix_result: Optional[Dict],
    input_mtime: Optional[float],
) -> None:
    """Persist the plan + global-design result + fingerprint. The
    ``files_written_mtimes`` map (for global-design output validation)
    is computed from ``global_fix_result['files_written']`` so a later
    invalidation check can confirm those files still exist."""
    fp = cache_path(workspace_root)
    fp.parent.mkdir(parents=True, exist_ok=True)
    files_written = (global_fix_result or {}).get("files_written") or []
    written_mtimes: Dict[str, float] = {}
    for rel in files_written:
        target = workspace_root / rel
        try:
            written_mtimes[rel] = target.stat().st_mtime
        except OSError:
            continue
    payload = {
        "saved_at": datetime.utcnow().isoformat(),
        "input_mtime": input_mtime,
        "plan": plan,
        "global_fix_result": global_fix_result,
        "files_written_mtimes": written_mtimes,
    }
    fp.write_text(json.dumps(payload, indent=2))


def is_cache_valid(
    cached: Dict,
    *,
    current_input_mtime: Optional[float],
    workspace_root: Path,
) -> tuple:
    """Decide whether the cached plan + global-design result are
    still usable. Returns ``(valid, reason)``.

    Invalid when:
      1. Any watched input file is newer than the recorded mtime.
      2. Any previously-written global-design output file no longer
         exists OR has been mtime-clobbered (the user / another
         tool overwrote it since we wrote it).
    """
    recorded = cached.get("input_mtime")
    if recorded is None:
        return (False, "cache missing input_mtime")
    if current_input_mtime is None:
        return (False, "no input files to fingerprint")
    if current_input_mtime > recorded + 0.001:
        return (False, "input files changed since last review")

    files_written_mtimes = cached.get("files_written_mtimes") or {}
    for rel, recorded_mtime in files_written_mtimes.items():
        target = workspace_root / rel
        if not target.exists():
            return (False, f"global-design output removed: {rel}")
        try:
            now_mtime = target.stat().st_mtime
        except OSError:
            return (False, f"global-design output stat failed: {rel}")
        # If the file mtime is now EARLIER than what we recorded,
        # something weird happened (git checkout, restore, etc.) —
        # safer to redo.
        if now_mtime < recorded_mtime - 0.001:
            return (False, f"global-design output mtime older: {rel}")
    return (True, "")
