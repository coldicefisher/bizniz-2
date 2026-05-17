"""Single source of truth for "where do ephemeral build files go?"

**Three kinds of build state, three answers:**

1. **Persistent project state** — ``~/bizniz_projects/<slug>/`` and
   the per-run state under ``.bizniz/runs/<job_id>/``. NEVER comes
   through this module. Resume + cost ledger + perf analyzer all
   depend on those files surviving forever.

2. **Ephemeral build files** — docker test exec dirs, transient
   build logs, MCP config tempfiles, anything that's safe to delete
   after the build is done. THIS module owns the location.

3. **Test artifacts** — pytest's ``tmp_path`` fixture. Self-cleaning;
   not our problem.

**Default ephemeral root** (in order of preference):

1. ``$BIZNIZ_EPHEMERAL_ROOT`` if set — operator override.
2. ``$XDG_RUNTIME_DIR/bizniz/`` if XDG_RUNTIME_DIR is set (Linux —
   tmpfs that the OS auto-cleans at logout).
3. ``/tmp/bizniz/`` as a last-resort fallback.

Why not ``Path.cwd() / ".bizniz" / "exec"`` (the old DockerPytestEnv
default)? The 2026-05-17 incident: 774 ``run_*`` dirs accumulated in
the bizniz repo because nothing cleaned them. Worse, docker created
``__pycache__`` subdirs as root, so the host user couldn't delete
them without docker's help. Moving exec out of the repo root means
``rm -rf $XDG_RUNTIME_DIR/bizniz`` cleans everything in one shot.

**Cleanup is best-effort.** Functions here never raise — a stale dir
that won't delete (root-owned, in-use) gets logged and skipped. The
operator-facing CLI in ``bizniz.cleanup`` handles root-owned cases
by re-running cleanup through docker.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Iterator, List

log = logging.getLogger(__name__)


def get_ephemeral_root() -> Path:
    """Return the canonical root for ephemeral build files.

    Idempotent — creates the directory if missing. Result is cached
    per-process; subsequent calls return the same Path.
    """
    cached = getattr(get_ephemeral_root, "_cached", None)
    if cached is not None:
        return cached

    override = os.environ.get("BIZNIZ_EPHEMERAL_ROOT")
    if override:
        root = Path(override).expanduser()
    else:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            root = Path(xdg) / "bizniz"
        else:
            root = Path("/tmp") / "bizniz"

    root.mkdir(parents=True, exist_ok=True)
    get_ephemeral_root._cached = root  # type: ignore[attr-defined]
    return root


def get_exec_root() -> Path:
    """Where docker-pytest ``run_*`` dirs live."""
    root = get_ephemeral_root() / "exec"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_log_dir() -> Path:
    """Where v2_build redirects stdout/stderr. One file per build."""
    root = get_ephemeral_root() / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_log_path(project_slug: str) -> Path:
    """Return ``<log_dir>/<slug>_<timestamp>.log`` — caller redirects
    to this. Single naming convention across all entry points."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    return get_log_dir() / f"{project_slug}_{ts}.log"


def iter_stale(
    root: Path,
    max_age_hours: float = 24.0,
) -> Iterator[Path]:
    """Yield direct children of ``root`` whose mtime is older than
    ``max_age_hours``. Used by cleanup_stale + the CLI.

    Safe on missing root (yields nothing).
    """
    if not root.exists():
        return
    cutoff = time.time() - max_age_hours * 3600.0
    for entry in root.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                yield entry
        except OSError:
            # Stat raced with deletion — skip silently.
            continue


def remove_path(path: Path) -> bool:
    """Best-effort recursive delete. Tries ``shutil.rmtree`` first;
    on permission error, attempts via docker (handles the common
    "container created it as root" case).

    Returns True on success, False on failure. Never raises.
    """
    if not path.exists():
        return True
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except PermissionError:
        return _docker_rm(path)
    except OSError as e:
        log.warning("ephemeral.remove_path(%s) failed: %s", path, e)
        return False


def _docker_rm(path: Path) -> bool:
    """Fallback delete via ``docker run --rm -v parent:/clean alpine
    rm -rf /clean/<name>``. Handles root-owned files from container
    bind-mounts. Silent no-op if docker isn't available."""
    import subprocess
    if shutil.which("docker") is None:
        return False
    parent = path.parent
    name = path.name
    if not parent.exists():
        return False
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{parent}:/clean",
             "alpine", "sh", "-c", f"rm -rf /clean/{name}"],
            capture_output=True, text=True, timeout=60,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def cleanup_stale(
    *,
    max_age_hours: float = 24.0,
    include_exec: bool = True,
    include_logs: bool = True,
) -> dict:
    """Prune stale exec dirs + logs. Returns ``{kind: removed_count,
    failed_count}`` so callers can log it.

    Safe to call from a DONE hook — never raises, never touches
    project state (``~/bizniz_projects/.../.bizniz/runs/``).
    """
    summary = {"exec_removed": 0, "exec_failed": 0,
               "logs_removed": 0, "logs_failed": 0}
    if include_exec:
        for entry in iter_stale(get_exec_root(), max_age_hours):
            if remove_path(entry):
                summary["exec_removed"] += 1
            else:
                summary["exec_failed"] += 1
    if include_logs:
        for entry in iter_stale(get_log_dir(), max_age_hours):
            if remove_path(entry):
                summary["logs_removed"] += 1
            else:
                summary["logs_failed"] += 1
    return summary


def reset_cache_for_testing() -> None:
    """Drop the cached ephemeral root. Tests need this when they
    monkeypatch env vars between cases."""
    if hasattr(get_ephemeral_root, "_cached"):
        delattr(get_ephemeral_root, "_cached")
