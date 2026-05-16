"""Tests for the retry-with-reconnect wrapper in project_db.py.

Surfaced 2026-05-15 during crm_v1 M5: ``project.db`` reports
"readonly database" on UPDATE after hours of activity, even though
the file is actually writable from a separate process. Workaround:
catch + reconnect + retry once.

Tests pin the wrapper's contract:
  - happy path passes through transparently (real sqlite)
  - readonly OperationalError → reconnect + retry once (mocked)
  - other OperationalErrors re-raise unchanged
  - retry succeeds when reconnect clears the issue
  - row_factory survives reconnect
  - reconnect re-chmods defensively

sqlite3.Connection methods are read-only (can't monkey-patch
.execute), so retry-path tests mock at the ``sqlite3.connect()``
boundary.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.project.project_db import _RetryingConnection


def _make_db(tmp_path) -> Path:
    """Real sqlite db with a basic table for happy-path tests."""
    db_dir = tmp_path / ".bizniz"
    db_dir.mkdir()
    db_path = db_dir / "project.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    conn.commit()
    conn.close()
    return db_path


# ── Happy path against real sqlite ─────────────────────────────────


class TestHappyPath:
    def test_execute_passthrough(self, tmp_path):
        db_path = _make_db(tmp_path)
        rc = _RetryingConnection(str(db_path), str(db_path.parent))
        rc.execute("INSERT INTO t (val) VALUES (?)", ("hello",))
        rc.commit()
        check = sqlite3.connect(str(db_path))
        row = check.execute("SELECT val FROM t WHERE id=1").fetchone()
        assert row == ("hello",)

    def test_executemany_passthrough(self, tmp_path):
        db_path = _make_db(tmp_path)
        rc = _RetryingConnection(str(db_path), str(db_path.parent))
        rc.executemany(
            "INSERT INTO t (val) VALUES (?)",
            [("a",), ("b",), ("c",)],
        )
        rc.commit()
        check = sqlite3.connect(str(db_path))
        n = check.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        assert n == 3

    def test_row_factory_passthrough(self, tmp_path):
        db_path = _make_db(tmp_path)
        rc = _RetryingConnection(str(db_path), str(db_path.parent))
        rc.row_factory = sqlite3.Row
        rc.execute("INSERT INTO t (val) VALUES (?)", ("x",))
        rc.commit()
        row = rc.execute("SELECT val FROM t").fetchone()
        assert row["val"] == "x"
        assert row[0] == "x"

    def test_unknown_attribute_forwards(self, tmp_path):
        db_path = _make_db(tmp_path)
        rc = _RetryingConnection(str(db_path), str(db_path.parent))
        # ``in_transaction`` is a real sqlite3.Connection property.
        assert rc.in_transaction is False

    def test_close_idempotent(self, tmp_path):
        db_path = _make_db(tmp_path)
        rc = _RetryingConnection(str(db_path), str(db_path.parent))
        rc.close()
        rc.close()  # second close shouldn't raise


# ── Retry path (mocked) ────────────────────────────────────────────


class _FakeConn:
    """Mock connection that can be configured to fail on first call
    then succeed, etc. ``execute``/``commit``/etc. dispatch to the
    ``behavior`` list (one entry per call, exhaustion repeats last)."""

    def __init__(self, behaviors=None):
        # behaviors: list of callables or sentinel values
        self.behaviors = list(behaviors or [])
        self.calls = []
        self.row_factory = None
        self.closed = False

    def _next(self, method, *a, **kw):
        self.calls.append((method, a, kw))
        if not self.behaviors:
            return MagicMock()  # default success
        # Pop one behavior (or reuse the last if exhausted).
        if len(self.behaviors) > 1:
            b = self.behaviors.pop(0)
        else:
            b = self.behaviors[0]
        if isinstance(b, BaseException):
            raise b
        if callable(b):
            return b(*a, **kw)
        return b  # passthrough value

    def execute(self, sql, *a, **kw):
        return self._next("execute", sql, *a, **kw)

    def executemany(self, sql, *a, **kw):
        return self._next("executemany", sql, *a, **kw)

    def executescript(self, sql, *a, **kw):
        return self._next("executescript", sql, *a, **kw)

    def commit(self):
        return self._next("commit")

    def close(self):
        self.closed = True


def _readonly_error():
    return sqlite3.OperationalError("attempt to write a readonly database")


class TestRetryPath:
    def test_readonly_then_success_via_reconnect(self):
        """First call raises readonly; reconnect returns a fresh
        conn whose execute() works; second call returns sentinel."""
        first = _FakeConn(behaviors=[_readonly_error()])
        second = _FakeConn(behaviors=["success-result"])

        with patch(
            "bizniz.project.project_db.sqlite3.connect",
            side_effect=[first, second],
        ):
            rc = _RetryingConnection("/x/db", "/x")
            # Initial _open() consumed the first conn.
            assert rc._conn is first
            result = rc.execute("INSERT INTO t VALUES (1)")
        assert result == "success-result"
        # Reconnected → underlying is now `second`.
        assert rc._conn is second
        # First conn was closed during reconnect.
        assert first.closed is True

    def test_persistent_readonly_re_raises(self):
        # Both first AND reconnected conn keep returning readonly.
        bad1 = _FakeConn(behaviors=[_readonly_error()])
        bad2 = _FakeConn(behaviors=[_readonly_error()])
        with patch(
            "bizniz.project.project_db.sqlite3.connect",
            side_effect=[bad1, bad2],
        ):
            rc = _RetryingConnection("/x/db", "/x")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                rc.execute("INSERT INTO t VALUES (1)")

    def test_non_readonly_operational_error_does_not_retry(self):
        # "database is locked" is a different OperationalError. Don't
        # retry — propagate immediately.
        conn = _FakeConn(behaviors=[
            sqlite3.OperationalError("database is locked"),
        ])
        with patch(
            "bizniz.project.project_db.sqlite3.connect",
            return_value=conn,
        ):
            rc = _RetryingConnection("/x/db", "/x")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rc.execute("INSERT INTO t VALUES (1)")
        # No reconnect → conn not closed.
        assert conn.closed is False

    def test_other_exception_types_propagate(self):
        conn = _FakeConn(behaviors=[
            sqlite3.IntegrityError("constraint failed"),
        ])
        with patch(
            "bizniz.project.project_db.sqlite3.connect",
            return_value=conn,
        ):
            rc = _RetryingConnection("/x/db", "/x")
            with pytest.raises(sqlite3.IntegrityError):
                rc.execute("INSERT INTO t VALUES (1)")

    def test_commit_retries_too(self):
        first = _FakeConn(behaviors=[_readonly_error()])
        second = _FakeConn(behaviors=["commit-ok"])
        with patch(
            "bizniz.project.project_db.sqlite3.connect",
            side_effect=[first, second],
        ):
            rc = _RetryingConnection("/x/db", "/x")
            result = rc.commit()
        assert result == "commit-ok"

    def test_executemany_retries(self):
        first = _FakeConn(behaviors=[_readonly_error()])
        second = _FakeConn(behaviors=["em-ok"])
        with patch(
            "bizniz.project.project_db.sqlite3.connect",
            side_effect=[first, second],
        ):
            rc = _RetryingConnection("/x/db", "/x")
            result = rc.executemany("INSERT INTO t VALUES (?)", [(1,)])
        assert result == "em-ok"

    def test_row_factory_preserved_across_reconnect(self):
        first = _FakeConn(behaviors=[_readonly_error()])
        first.row_factory = sqlite3.Row
        second = _FakeConn(behaviors=["ok"])
        with patch(
            "bizniz.project.project_db.sqlite3.connect",
            side_effect=[first, second],
        ):
            rc = _RetryingConnection("/x/db", "/x")
            # Set row_factory on the wrapper before the failing call.
            rc.row_factory = sqlite3.Row
            rc.execute("INSERT INTO t VALUES (1)")
        # After reconnect, row_factory survived.
        assert second.row_factory is sqlite3.Row


class TestReconnectChmod:
    def test_reconnect_chmods_dir_and_file(self):
        conn = _FakeConn(behaviors=[_readonly_error(), "ok"])
        with patch(
            "bizniz.project.project_db.sqlite3.connect",
            side_effect=[conn, _FakeConn(behaviors=["ok"])],
        ), patch(
            "bizniz.project.project_db.os.chmod",
        ) as chm:
            rc = _RetryingConnection("/x/db", "/x")
            rc.execute("INSERT INTO t VALUES (1)")
        paths_chmoded = {call.args[0] for call in chm.call_args_list}
        assert "/x" in paths_chmoded
        assert "/x/db" in paths_chmoded

    def test_chmod_oserror_swallowed(self):
        conn1 = _FakeConn(behaviors=[_readonly_error()])
        conn2 = _FakeConn(behaviors=["ok"])
        with patch(
            "bizniz.project.project_db.sqlite3.connect",
            side_effect=[conn1, conn2],
        ), patch(
            "bizniz.project.project_db.os.chmod",
            side_effect=OSError("denied"),
        ):
            rc = _RetryingConnection("/x/db", "/x")
            # Should NOT raise — chmod failure during reconnect is
            # non-fatal and the retry should still happen.
            rc.execute("INSERT INTO t VALUES (1)")
