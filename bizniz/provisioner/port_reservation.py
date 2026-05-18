"""Cross-process port reservation for parallel builds.

Two provisioners running concurrently both ran ``_find_free_port``
independently, both saw FusionAuth's port 9011 free (because neither
compose stack had come up yet), and both picked 9011 — the second
``docker compose up`` failed with "port is already allocated."
This module closes that race.

Mechanism:
- A JSON file at ``$BIZNIZ_PROJECTS_ROOT/.port_reservations.json``
  (default ``~/bizniz_projects/.port_reservations.json``) holds an
  array of ``{"port", "project_slug", "claimed_at", "expires_at"}``
  entries.
- ``reserve_ports(slug, ports)`` opens the file under ``fcntl.flock``
  (exclusive), prunes expired entries, appends the new ones, writes
  it back, and releases the lock. Two concurrent callers serialize
  through the lock — the second one sees the first one's claims and
  picks different ports.
- ``active_reservations()`` reads the registry under a shared lock
  and returns the set of currently-reserved ports.
- TTL is 1 hour by default. That's well over the typical
  provisioner→compose-up gap (seconds to minutes) but short enough
  that a crashed/abandoned build doesn't lock ports forever.
- Release on success is optional — if it's missed, TTL expiry covers.

Cross-platform note: ``fcntl.flock`` is POSIX-only. On Windows we
fall back to a sentinel ``.lock`` file (best-effort; bizniz is
Linux-first per CLAUDE.md, so this is a hedge not a guarantee).
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:
    import fcntl  # POSIX
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore
    _HAS_FCNTL = False


_DEFAULT_TTL_S = 60 * 60  # 1 hour


def _projects_root() -> Path:
    """Where the registry lives. Honors ``BIZNIZ_PROJECTS_ROOT`` so
    tests can swap it without touching ``~``."""
    return Path(
        os.environ.get("BIZNIZ_PROJECTS_ROOT", str(Path.home() / "bizniz_projects"))
    )


def _registry_path() -> Path:
    return _projects_root() / ".port_reservations.json"


def _lock_path() -> Path:
    return _projects_root() / ".port_reservations.lock"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


@contextmanager
def _exclusive_lock():
    """Acquire an exclusive cross-process lock. Yields a handle."""
    _projects_root().mkdir(parents=True, exist_ok=True)
    lock_file = _lock_path()
    fh = open(lock_file, "a+")
    try:
        if _HAS_FCNTL and fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield fh
    finally:
        try:
            if _HAS_FCNTL and fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _load_pruned(now: Optional[datetime] = None) -> List[Dict]:
    """Read the registry, drop expired entries. Returns the live list.
    Assumes caller holds the lock."""
    path = _registry_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    items = raw.get("reservations", []) if isinstance(raw, dict) else []
    if now is None:
        now = datetime.now(timezone.utc)
    live: List[Dict] = []
    for item in items:
        exp = _parse_iso(item.get("expires_at", ""))
        if exp is None:
            continue
        if exp > now:
            live.append(item)
    return live


def _write(items: List[Dict]) -> None:
    """Write the registry. Assumes caller holds the lock."""
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"reservations": items}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


# ── Public API ────────────────────────────────────────────────────


def active_reservations() -> Dict[int, str]:
    """Return ``{port: project_slug}`` for currently-reserved ports.

    Side effect: prunes expired entries and rewrites the file. (Cheap
    — file is tiny — and keeps the registry from growing indefinitely.)
    """
    with _exclusive_lock():
        live = _load_pruned()
        _write(live)
    return {int(item["port"]): str(item["project_slug"]) for item in live}


def reserve_ports(
    project_slug: str,
    ports: Iterable[int],
    ttl_s: float = _DEFAULT_TTL_S,
) -> None:
    """Claim ``ports`` for ``project_slug``. Atomic — concurrent callers
    serialize through the file lock.

    Raises:
        ValueError — if any port is already reserved by a DIFFERENT
        project (caller is using stale data; the right move is to
        re-allocate, not silently double-book).
    """
    ports_list = sorted(set(int(p) for p in ports))
    if not ports_list:
        return
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_s)
    with _exclusive_lock():
        live = _load_pruned(now)
        live_by_port: Dict[int, str] = {int(it["port"]): str(it["project_slug"]) for it in live}
        conflicts = [
            p for p in ports_list
            if p in live_by_port and live_by_port[p] != project_slug
        ]
        if conflicts:
            raise ValueError(
                f"reserve_ports({project_slug}): ports {conflicts} already "
                f"reserved by another project — caller used stale data."
            )
        # Drop any prior reservations for THIS slug + add the new ones.
        live = [it for it in live if it.get("project_slug") != project_slug]
        for p in ports_list:
            live.append({
                "port": p,
                "project_slug": project_slug,
                "claimed_at": _now_iso(),
                "expires_at": expires.isoformat(),
            })
        _write(live)


def release_ports(project_slug: str) -> None:
    """Remove all reservations for ``project_slug``. Best-effort; if
    never called, TTL expiry covers."""
    with _exclusive_lock():
        live = _load_pruned()
        live = [it for it in live if it.get("project_slug") != project_slug]
        _write(live)
