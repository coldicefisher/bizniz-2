"""DesignLock — establish-once design system that persists across milestones.

The first milestone (M1) emits a design plan + writes tokens +
primitives (palette, typography, tailwind.config, src/index.css,
src/components/ui/*). Without this module, M2-M5 would re-derive
the design every milestone (plan_cache misses on legitimate
IMPLEMENT-phase writes that bump src/**/* mtimes).

This module saves a lock file at
``<workspace>/.bizniz/design_lock.json`` after M1's global_design
completes. M2-M5 check for the lock at the top of UX phase and
SKIP both code_review + apply_global_design when it's present.

Result: the design system is established once and stable thereafter.
No more visual drift between milestones, no more ~15min/milestone
spent re-deriving the same palette.

When to invalidate / re-establish:

  - User explicitly requests redesign (constructor flag
    ``force_redesign=True`` on ProUXDesigner).
  - User deletes the lock file manually.
  - Future: Planner emits a milestone with ``redesign_after=True``
    (parallel to the existing ``refactor_after``).

What lives in the lock:

  - The design ``plan`` produced by code_review (so downstream
    prompts that reference it still work)
  - The ``global_fix_result`` (files_written, status, tailwind_wired,
    notes — same shape Coder returns)
  - ``files_managed`` (the keys of global_fix_result.files_written,
    so the per-view loop knows which files NOT to touch)
  - ``established_at`` (ISO timestamp for diagnostics)
  - ``milestone_index`` (which milestone established the design;
    typically 0 = M1)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


LOCK_FILENAME = "design_lock.json"
LOCK_VERSION = 1


class DesignLock(BaseModel):
    """Persistent record of the design system established at M1."""

    version: int = LOCK_VERSION
    established_at: datetime = Field(default_factory=datetime.utcnow)
    milestone_index: int = 0
    plan: Dict = Field(default_factory=dict)
    global_fix_result: Dict = Field(default_factory=dict)
    files_managed: List[str] = Field(default_factory=list)


def lock_path(workspace_root: Path) -> Path:
    return workspace_root / ".bizniz" / LOCK_FILENAME


def load_lock(workspace_root: Path) -> Optional[DesignLock]:
    """Return the saved DesignLock or None if no lock exists / file
    is unreadable / version doesn't match."""
    fp = lock_path(workspace_root)
    if not fp.exists():
        return None
    try:
        payload = json.loads(fp.read_text())
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != LOCK_VERSION:
        return None
    try:
        return DesignLock.model_validate(payload)
    except Exception:
        return None


def save_lock(workspace_root: Path, lock: DesignLock) -> None:
    """Persist the design lock. Creates parent dir if missing."""
    fp = lock_path(workspace_root)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(lock.model_dump_json(indent=2))


def remove_lock(workspace_root: Path) -> bool:
    """Delete the lock if present. Returns True if a file was
    removed, False if no lock existed. Used by ``--redesign`` flag
    and the future Planner-emitted ``redesign_after`` milestone hook."""
    fp = lock_path(workspace_root)
    if not fp.exists():
        return False
    try:
        fp.unlink()
        return True
    except Exception:
        return False
