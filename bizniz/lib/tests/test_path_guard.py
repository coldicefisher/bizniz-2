"""Tests for the path-guard helper (2026-05-17, ephemeral hygiene
phase A)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.lib.path_guard import is_real_filesystem_path


class TestIsRealFilesystemPath:
    def test_existing_absolute_path_passes(self, tmp_path):
        assert is_real_filesystem_path(tmp_path) is True

    def test_subpath_under_existing_parent_passes(self, tmp_path):
        # Doesn't exist yet, but parent does — like a new project root.
        assert is_real_filesystem_path(tmp_path / "new_project") is True

    def test_string_path_under_real_parent_passes(self, tmp_path):
        # String form should also work — Path() coerces.
        assert is_real_filesystem_path(str(tmp_path / "foo")) is True

    def test_none_rejected(self):
        assert is_real_filesystem_path(None) is False

    def test_relative_path_rejected(self):
        # Relative paths are ambiguous and CWD-dependent — reject.
        assert is_real_filesystem_path(Path("backend/tests")) is False
        assert is_real_filesystem_path("relative/path") is False

    def test_nonexistent_parent_rejected(self):
        # Parent must exist for the write to land in a sane place.
        assert is_real_filesystem_path("/no/such/parent/foo") is False

    def test_magicmock_workspace_root_rejected(self):
        # The exact incident: AuthAgent did Path(MagicMock(spec=BaseWorkspace).root)
        # and Path coerced to "MagicMock/mock.root/<id>". Guard catches it.
        mock_ws_root = MagicMock().root
        assert is_real_filesystem_path(mock_ws_root) is False

    def test_repr_with_mock_substring_rejected(self):
        # Even if a path string accidentally CONTAINS "MagicMock" or
        # "<Mock" or "mock.root", refuse — these are mock leaks.
        assert is_real_filesystem_path("/tmp/MagicMock/foo") is False
        assert is_real_filesystem_path("/tmp/<MockedClass>/foo") is False
        assert is_real_filesystem_path("/tmp/mock.root/foo") is False

    def test_bare_magicmock_rejected(self):
        assert is_real_filesystem_path(MagicMock()) is False
