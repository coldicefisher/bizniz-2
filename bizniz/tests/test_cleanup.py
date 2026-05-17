"""Tests for ``python -m bizniz.cleanup`` (2026-05-17, phase C)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from bizniz import cleanup
from bizniz.lib import ephemeral


@pytest.fixture(autouse=True)
def _isolated_ephemeral(tmp_path, monkeypatch):
    """Sandbox the ephemeral root so cleanup tests don't touch the
    operator's real $XDG_RUNTIME_DIR/bizniz."""
    monkeypatch.setenv("BIZNIZ_EPHEMERAL_ROOT", str(tmp_path / "ephem"))
    ephemeral.reset_cache_for_testing()
    yield
    ephemeral.reset_cache_for_testing()


@pytest.fixture
def isolated_projects_root(tmp_path, monkeypatch):
    """Sandbox ~/bizniz_projects/ so the --runs path is testable."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("BIZNIZ_PROJECTS_ROOT", str(root))
    return root


def _make_stale(path: Path, hours: float = 48.0) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    old_t = time.time() - hours * 3600
    os.utime(path, (old_t, old_t))
    return path


# ── --exec / --logs ──────────────────────────────────────────────


class TestExecAndLogsCleanup:
    def test_prunes_stale_exec(self, capsys):
        stale = _make_stale(ephemeral.get_exec_root() / "run_old")
        fresh = ephemeral.get_exec_root() / "run_fresh"
        fresh.mkdir()

        rc = cleanup.main(["--exec", "--max-age-hours", "1"])
        assert rc == 0
        assert not stale.exists()
        assert fresh.exists()

        out = capsys.readouterr().out
        assert "exec: 1 removed" in out

    def test_prunes_stale_logs(self, capsys):
        stale = ephemeral.get_log_dir() / "stale.log"
        stale.write_text("old")
        old_t = time.time() - 48 * 3600
        os.utime(stale, (old_t, old_t))

        fresh = ephemeral.get_log_dir() / "fresh.log"
        fresh.write_text("new")

        rc = cleanup.main(["--logs", "--max-age-hours", "1"])
        assert rc == 0
        assert not stale.exists()
        assert fresh.exists()

    def test_dry_run_does_not_delete(self, capsys):
        stale = _make_stale(ephemeral.get_exec_root() / "run_old")
        cleanup.main(["--exec", "--dry-run", "--max-age-hours", "1"])
        assert stale.exists()
        out = capsys.readouterr().out
        assert "dry-run" in out


# ── --runs ───────────────────────────────────────────────────────


class TestRunsCleanup:
    def test_keeps_most_recent_n(self, isolated_projects_root, capsys):
        project_runs = (
            isolated_projects_root / "recipe_v2" / ".bizniz" / "runs"
        )
        project_runs.mkdir(parents=True)
        # Five runs, increasing mtimes.
        for i, name in enumerate(["a", "b", "c", "d", "e"]):
            d = project_runs / name
            d.mkdir()
            os.utime(d, (1000 + i, 1000 + i))

        rc = cleanup.main(
            ["--runs", "--project", "recipe_v2", "--keep", "2"]
        )
        assert rc == 0
        remaining = sorted(p.name for p in project_runs.iterdir())
        # Oldest three (a/b/c) removed; newest two kept.
        assert remaining == ["d", "e"]

    def test_no_op_when_within_keep(self, isolated_projects_root):
        runs = (
            isolated_projects_root / "x" / ".bizniz" / "runs"
        )
        runs.mkdir(parents=True)
        (runs / "only_one").mkdir()
        cleanup.main(["--runs", "--project", "x", "--keep", "3"])
        assert (runs / "only_one").exists()

    def test_all_projects_when_no_project_arg(
        self, isolated_projects_root, capsys,
    ):
        for slug in ("p1", "p2"):
            runs = isolated_projects_root / slug / ".bizniz" / "runs"
            runs.mkdir(parents=True)
            for i, name in enumerate(["old", "new"]):
                d = runs / name
                d.mkdir()
                os.utime(d, (1000 + i, 1000 + i))

        cleanup.main(["--runs", "--keep", "1"])

        for slug in ("p1", "p2"):
            remaining = sorted(
                p.name for p in
                (isolated_projects_root / slug / ".bizniz" / "runs").iterdir()
            )
            assert remaining == ["new"]


class TestArgs:
    def test_no_action_flag_errors(self):
        with pytest.raises(SystemExit):
            cleanup.main([])

    def test_all_implies_every_target(
        self, isolated_projects_root, capsys,
    ):
        _make_stale(ephemeral.get_exec_root() / "run_old")
        runs = isolated_projects_root / "p" / ".bizniz" / "runs"
        runs.mkdir(parents=True)
        for i, n in enumerate(["a", "b", "c"]):
            d = runs / n
            d.mkdir()
            os.utime(d, (1000 + i, 1000 + i))

        rc = cleanup.main(["--all", "--max-age-hours", "1", "--keep", "1"])
        assert rc == 0
        # exec gone, only newest run kept.
        assert not (ephemeral.get_exec_root() / "run_old").exists()
        remaining = sorted(p.name for p in runs.iterdir())
        assert remaining == ["c"]
