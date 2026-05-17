"""Resolve where per-run agent state lives on disk.

Originally state lived under ``<project>/docs/runs/<job_id>/``. As
of 2026-05-16 (roadmap item 8), it moved to
``<project>/.bizniz/runs/<job_id>/`` to keep ``<project>/docs/``
reserved for human-readable engineering documentation.

This module is the canonical resolver. Writers ALWAYS use the new
path; readers fall back to the legacy path when the new one is
absent so existing projects keep resuming.
"""
from __future__ import annotations

from pathlib import Path


NEW_RUNS_REL = (".bizniz", "runs")
LEGACY_RUNS_REL = ("docs", "runs")


def writes_runs_root(project_root: Path) -> Path:
    """Return the path NEW state should be written to. Always the
    new ``.bizniz/runs/`` location — never the legacy one."""
    return Path(project_root) / NEW_RUNS_REL[0] / NEW_RUNS_REL[1]


def resolve_runs_root(project_root: Path) -> Path:
    """Return the path readers should consult.

    Lookup order:
    1. If ``<project>/.bizniz/runs/`` exists, return it.
    2. Else if ``<project>/docs/runs/`` exists (legacy), return that.
    3. Else return ``<project>/.bizniz/runs/`` (new path; caller may
       create it on first write).
    """
    project_root = Path(project_root)
    new_path = project_root / NEW_RUNS_REL[0] / NEW_RUNS_REL[1]
    if new_path.is_dir():
        return new_path
    legacy_path = project_root / LEGACY_RUNS_REL[0] / LEGACY_RUNS_REL[1]
    if legacy_path.is_dir():
        return legacy_path
    return new_path
