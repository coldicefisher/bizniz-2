"""Tests for the runs-path resolver (item 8A)."""
from __future__ import annotations

from pathlib import Path

import pytest

from bizniz.driver.runs_paths import (
    LEGACY_RUNS_REL, NEW_RUNS_REL,
    resolve_runs_root, writes_runs_root,
)


class TestWritesRunsRoot:
    def test_returns_new_path(self, tmp_path):
        p = writes_runs_root(tmp_path)
        assert p == tmp_path / ".bizniz" / "runs"

    def test_does_not_create_directory(self, tmp_path):
        # Pure path computation — caller is responsible for mkdir.
        p = writes_runs_root(tmp_path)
        assert not p.exists()


class TestResolveRunsRoot:
    def test_prefers_new_when_both_exist(self, tmp_path):
        (tmp_path / ".bizniz" / "runs").mkdir(parents=True)
        (tmp_path / "docs" / "runs").mkdir(parents=True)
        resolved = resolve_runs_root(tmp_path)
        assert resolved == tmp_path / ".bizniz" / "runs"

    def test_falls_back_to_legacy_when_new_absent(self, tmp_path):
        (tmp_path / "docs" / "runs").mkdir(parents=True)
        resolved = resolve_runs_root(tmp_path)
        assert resolved == tmp_path / "docs" / "runs"

    def test_returns_new_when_neither_exists(self, tmp_path):
        # New path doesn't exist yet — caller will create.
        resolved = resolve_runs_root(tmp_path)
        assert resolved == tmp_path / ".bizniz" / "runs"
        # No directory was created.
        assert not resolved.exists()

    def test_treats_file_at_new_path_as_absent(self, tmp_path):
        # Defensive: if something put a FILE at .bizniz/runs, treat
        # it as "not a runs root" and try legacy.
        (tmp_path / ".bizniz").mkdir()
        (tmp_path / ".bizniz" / "runs").write_text("oops", "utf-8")
        (tmp_path / "docs" / "runs").mkdir(parents=True)
        resolved = resolve_runs_root(tmp_path)
        assert resolved == tmp_path / "docs" / "runs"


class TestPathConstants:
    def test_new_path_rel(self):
        assert NEW_RUNS_REL == (".bizniz", "runs")

    def test_legacy_path_rel(self):
        assert LEGACY_RUNS_REL == ("docs", "runs")
