"""Tests for the ephemeral-files module (2026-05-17, phase B)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from bizniz.lib import ephemeral


@pytest.fixture(autouse=True)
def _reset_cache():
    """Drop the cached ephemeral root before AND after each test —
    env-var changes per test must not leak across cases."""
    ephemeral.reset_cache_for_testing()
    yield
    ephemeral.reset_cache_for_testing()


class TestGetEphemeralRoot:
    def test_bizniz_ephemeral_root_wins(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom_ephemeral"
        monkeypatch.setenv("BIZNIZ_EPHEMERAL_ROOT", str(custom))
        # Even if XDG_RUNTIME_DIR is set, the explicit override wins.
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
        root = ephemeral.get_ephemeral_root()
        assert root == custom
        assert root.exists()

    def test_xdg_runtime_dir_used_when_no_override(
        self, tmp_path, monkeypatch,
    ):
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        monkeypatch.delenv("BIZNIZ_EPHEMERAL_ROOT", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        root = ephemeral.get_ephemeral_root()
        assert root == xdg / "bizniz"
        assert root.exists()

    def test_tmp_fallback_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("BIZNIZ_EPHEMERAL_ROOT", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        root = ephemeral.get_ephemeral_root()
        assert root == Path("/tmp") / "bizniz"

    def test_subroots_have_predictable_layout(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("BIZNIZ_EPHEMERAL_ROOT", str(tmp_path))
        assert ephemeral.get_exec_root() == tmp_path / "exec"
        assert ephemeral.get_log_dir() == tmp_path / "logs"

    def test_log_path_includes_slug_and_timestamp(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("BIZNIZ_EPHEMERAL_ROOT", str(tmp_path))
        p = ephemeral.make_log_path("recipe_v2")
        assert p.parent == tmp_path / "logs"
        assert p.name.startswith("recipe_v2_")
        assert p.name.endswith(".log")


class TestIterStale:
    def test_yields_only_old_entries(self, tmp_path):
        # Build 3 dirs: one fresh, two stale.
        for name in ("fresh", "old_1", "old_2"):
            (tmp_path / name).mkdir()
        # Backdate two of them.
        old_t = time.time() - 48 * 3600
        os.utime(tmp_path / "old_1", (old_t, old_t))
        os.utime(tmp_path / "old_2", (old_t, old_t))

        stale = list(ephemeral.iter_stale(tmp_path, max_age_hours=24))
        names = {p.name for p in stale}
        assert names == {"old_1", "old_2"}

    def test_missing_root_is_no_op(self, tmp_path):
        out = list(ephemeral.iter_stale(tmp_path / "does_not_exist"))
        assert out == []


class TestRemovePath:
    def test_removes_directory_tree(self, tmp_path):
        d = tmp_path / "victim"
        d.mkdir()
        (d / "x.txt").write_text("hi")
        assert ephemeral.remove_path(d) is True
        assert not d.exists()

    def test_removes_single_file(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("hi")
        assert ephemeral.remove_path(f) is True
        assert not f.exists()

    def test_missing_path_returns_true(self, tmp_path):
        # Idempotent — "deleting" something already gone is success.
        assert ephemeral.remove_path(tmp_path / "missing") is True


class TestCleanupStale:
    def test_removes_old_exec_and_log_entries(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("BIZNIZ_EPHEMERAL_ROOT", str(tmp_path))
        exec_root = ephemeral.get_exec_root()
        log_dir = ephemeral.get_log_dir()

        # Create one fresh + one stale in each.
        for parent, fresh_name, stale_name in [
            (exec_root, "run_fresh", "run_stale"),
            (log_dir, "fresh.log", "stale.log"),
        ]:
            (parent / fresh_name).mkdir(exist_ok=True) if not fresh_name.endswith(".log") else (parent / fresh_name).write_text("")
            stale_path = parent / stale_name
            if stale_name.endswith(".log"):
                stale_path.write_text("old")
            else:
                stale_path.mkdir(exist_ok=True)
            old_t = time.time() - 48 * 3600
            os.utime(stale_path, (old_t, old_t))

        summary = ephemeral.cleanup_stale(max_age_hours=24.0)
        assert summary["exec_removed"] == 1
        assert summary["logs_removed"] == 1
        assert summary["exec_failed"] == 0
        assert summary["logs_failed"] == 0

        # Fresh entries still there.
        assert (exec_root / "run_fresh").exists()
        assert (log_dir / "fresh.log").exists()

    def test_can_disable_either_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BIZNIZ_EPHEMERAL_ROOT", str(tmp_path))
        exec_root = ephemeral.get_exec_root()
        old = exec_root / "run_old"
        old.mkdir()
        old_t = time.time() - 48 * 3600
        os.utime(old, (old_t, old_t))

        summary = ephemeral.cleanup_stale(
            max_age_hours=24.0, include_exec=False,
        )
        assert summary["exec_removed"] == 0
        assert old.exists()
