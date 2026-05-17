"""Defensive helpers for code that writes to user-supplied paths.

Background — the 2026-05-17 incident:

A test fixture supplied ``MagicMock(spec=BaseWorkspace)`` as a
workspace. The production code under test does
``Path(self._workspace.root) / "tests" / "auth"`` and then
``.mkdir(parents=True, exist_ok=True)``. Because the MagicMock
auto-spec'd ``.root`` with ``__fspath__``, ``Path()`` coerced the
mock into a string like ``"MagicMock/mock.root/<id>"`` and
``.mkdir(parents=True)`` created that directory tree in the
current working directory. After many test runs, the bizniz repo
root had a ``MagicMock/`` directory with hundreds of files.

The guard below catches the case before any filesystem write
happens: a "real" filesystem path is one that's path-coercible,
absolute, AND whose parent already exists. A fake mock root
fails one of those three.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def is_real_filesystem_path(candidate: Any) -> bool:
    """Return True iff ``candidate`` looks like a real filesystem
    path: coercible to ``Path``, absolute, parent exists.

    Use this BEFORE any ``mkdir(parents=True)`` / ``write_text()``
    on a caller-supplied root, particularly when the caller might
    be a test passing a MagicMock.

    Returning False is conservative: a brand-new project root that
    doesn't exist yet AND whose parent doesn't exist yet would also
    fail. In practice every real bizniz root is under
    ``~/bizniz_projects/<slug>/``, so the parent (``~/bizniz_projects/``)
    is always present.
    """
    if candidate is None:
        return False
    try:
        p = Path(candidate)
    except TypeError:
        return False
    # Repr-shaped MagicMock leaks (``<MagicMock...`` substrings) —
    # always reject these even if they coerce.
    s = str(p)
    if "MagicMock" in s or "<Mock" in s or "mock.root" in s:
        return False
    if not p.is_absolute():
        return False
    if not p.parent.exists():
        return False
    return True
