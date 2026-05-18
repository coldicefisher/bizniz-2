"""Unit tests for the recipes migration hook in ``app.main.lifespan``.

These tests verify the BE-001-fix1 contract: the startup lifespan
runner invokes ``app.db.migrations.recipes.run_migration`` BEFORE
the closing full ``Base.metadata.create_all``. Ordering matters —
the original BE-001 order ran ``create_all`` first, which created
the recipes table without CHECK constraints (CodeReviewer finding)
because the migration's ``CREATE TABLE IF NOT EXISTS`` then no-oped
on the already-existing table. The fix runs the migration FIRST so
the CHECK constraints actually land; the closing ``create_all``
becomes a no-op for the recipes table and a safety net for any
other ORM-mapped tables.

The recipes migration's FK to ``users(id)`` still requires users to
exist when the migration runs, so the lifespan creates users via a
filtered ``create_all`` over just the User table BEFORE invoking
the migration. That filtered call is a lambda — distinct from the
full ``Base.metadata.create_all`` identity — so this test asserts
the full ``create_all`` runs AFTER the migration.

Strategy: replace the module-level ``engine`` with a fake whose
``begin()`` returns an async-context-managed connection that
records the order of ``run_sync`` callables it receives. Drive the
lifespan context manager and assert on the recorded order.

Exception-propagation is also covered: if the recipes migration
raises (e.g. the ``users`` FK target is somehow missing), the
process MUST ``sys.exit(1)`` rather than boot a half-migrated
container — same fail-fast contract that the existing
``create_all``-failure branch enforces.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest import mock

import pytest

from app import main as main_module
from app.db.base import Base
from app.db.migrations import recipes as recipes_migration


class _RecordingConn:
    """Async-conn double that records the callables passed to ``run_sync``.

    Mirrors the surface area the lifespan actually uses — just
    ``run_sync``. Returning ``None`` matches the real
    ``conn.run_sync`` for DDL callables that don't produce a value.
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def run_sync(self, fn: Any) -> None:
        self.calls.append(fn)
        return None


def _fake_engine_with_conn(conn: _RecordingConn) -> mock.MagicMock:
    """Build a fake engine whose ``begin()`` yields the given conn.

    The real ``engine.begin()`` is an async context manager; this
    mirrors the shape with ``@asynccontextmanager`` so the lifespan's
    ``async with engine.begin() as conn`` path runs unchanged.
    """

    @asynccontextmanager
    async def _begin() -> Any:
        yield conn

    fake = mock.MagicMock()
    fake.begin = _begin
    return fake


@pytest.fixture
def recording_conn(monkeypatch: pytest.MonkeyPatch) -> _RecordingConn:
    """Swap the lifespan's engine for a recorder and yield the conn."""
    conn = _RecordingConn()
    monkeypatch.setattr(main_module, "engine", _fake_engine_with_conn(conn))
    return conn


async def test_lifespan_invokes_recipes_migration(
    recording_conn: _RecordingConn,
) -> None:
    """Lifespan runs ``recipes.run_migration`` at startup.

    Locks the hook itself: removing the call from main.py would make
    this assertion fail and the regression would be caught here
    rather than at the next live-DB integration test.
    """
    async with main_module.lifespan(main_module.app):
        pass
    assert recipes_migration.run_migration in recording_conn.calls, (
        "lifespan must invoke app.db.migrations.recipes.run_migration "
        f"at startup; observed callables: {recording_conn.calls!r}"
    )


async def test_lifespan_runs_recipes_migration_before_full_create_all(
    recording_conn: _RecordingConn,
) -> None:
    """``run_migration`` precedes the closing full ``Base.metadata.create_all``.

    BE-001-fix1 contract: the migration must run BEFORE the
    unfiltered ``create_all`` so the recipes table is created WITH
    the four spec CHECK constraints (title/description length,
    prep/cook 0-1440, servings 1-1000). If ``create_all`` ran first
    it would create the recipes table without those constraints,
    and the migration's ``CREATE TABLE IF NOT EXISTS`` would silently
    no-op — leaving the live table unconstrained (the bug this fix
    corrects). The users-only filtered ``create_all`` that runs as a
    prelude to the migration is a separate callable (a lambda) and
    does not match ``Base.metadata.create_all`` by identity.
    """
    async with main_module.lifespan(main_module.app):
        pass

    create_all_idx = recording_conn.calls.index(Base.metadata.create_all)
    migration_idx = recording_conn.calls.index(recipes_migration.run_migration)
    assert migration_idx < create_all_idx, (
        f"recipes.run_migration (pos {migration_idx}) must run before "
        f"the full Base.metadata.create_all (pos {create_all_idx}); "
        f"observed order: {recording_conn.calls!r}"
    )


async def test_lifespan_exits_when_recipes_migration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``run_migration`` raises, the process ``sys.exit(1)``s.

    The existing ``create_all``-failure branch calls ``sys.exit(1)``
    rather than letting the exception escape — because exceptions
    raised inside a FastAPI lifespan are swallowed by Starlette and
    the server still starts. The recipes-migration call sits in the
    same ``try`` block and inherits the same contract: a failed
    migration must take the container down with a clear log line,
    not boot serving 5xx.
    """
    boom = RuntimeError("recipes migration failed (e.g. users table missing)")

    class _FailingConn:
        """Recipes migration call raises; create_all call succeeds."""

        def __init__(self) -> None:
            self._call_count = 0

        async def run_sync(self, fn: Any) -> None:
            self._call_count += 1
            if fn is recipes_migration.run_migration:
                raise boom
            return None

    conn = _FailingConn()
    monkeypatch.setattr(main_module, "engine", _fake_engine_with_conn(conn))

    with pytest.raises(SystemExit) as excinfo:
        async with main_module.lifespan(main_module.app):
            pass
    assert excinfo.value.code == 1, (
        "expected lifespan to sys.exit(1) on recipes-migration failure "
        f"(matching the create_all-failure contract); got exit code "
        f"{excinfo.value.code!r}"
    )
