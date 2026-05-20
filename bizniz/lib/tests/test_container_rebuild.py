"""Tests for the container_rebuild utility.

Subprocess calls are mocked — we never actually invoke docker.
Verifies trigger detection, mode selection (soft vs hard), error
surfacing, and no-op when compose args are missing.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.lib.container_rebuild import (
    RebuildResult, detect_changes, hash_trigger_files, maybe_rebuild,
)


# ── Hash + diff ────────────────────────────────────────────────────


class TestHashing:
    def test_hash_existing_file(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("pyjwt==2.10.0\n")
        h = hash_trigger_files(tmp_path)
        assert h["requirements.txt"]
        assert len(h["requirements.txt"]) == 64  # sha256 hex

    def test_missing_file_is_empty_string(self, tmp_path):
        h = hash_trigger_files(tmp_path)
        assert h["requirements.txt"] == ""

    def test_detect_changes_empty_to_nonempty(self):
        before = {"requirements.txt": ""}
        after = {"requirements.txt": "abc123"}
        assert detect_changes(before, after) == ["requirements.txt"]

    def test_detect_changes_nonempty_to_different(self):
        before = {"requirements.txt": "abc"}
        after = {"requirements.txt": "def"}
        assert detect_changes(before, after) == ["requirements.txt"]

    def test_detect_changes_unchanged_returns_empty(self):
        h = {"requirements.txt": "abc"}
        assert detect_changes(h, h) == []


# ── maybe_rebuild routing ──────────────────────────────────────────


class TestRebuildRouting:
    def test_no_compose_path_returns_noop(self, tmp_path):
        before = hash_trigger_files(tmp_path)
        result = maybe_rebuild(
            compose_path=None,
            service_name="backend",
            workspace_root=tmp_path,
            before_hashes=before,
        )
        assert not result.triggered
        assert result.mode == "none"

    def test_no_change_returns_noop(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        before = hash_trigger_files(tmp_path)
        # No change between before + after.
        result = maybe_rebuild(
            compose_path="/c.yml",
            service_name="backend",
            workspace_root=tmp_path,
            before_hashes=before,
        )
        assert not result.triggered

    def test_requirements_change_picks_soft_mode(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        before = hash_trigger_files(tmp_path)
        (tmp_path / "requirements.txt").write_text("fastapi\npyjwt\n")

        # Mock both subprocess.run + container-running check.
        def fake_run(cmd, *args, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "bizniz.lib.container_rebuild.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "bizniz.lib.container_rebuild._is_container_running",
            return_value=True,
        ):
            result = maybe_rebuild(
                compose_path="/c.yml",
                service_name="backend",
                workspace_root=tmp_path,
                before_hashes=before,
                health_timeout_s=0.5,
            )
        assert result.triggered
        assert result.mode == "soft"
        assert result.success is True

    def test_dockerfile_change_picks_hard_mode(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
        before = hash_trigger_files(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM python:3.13\n")

        def fake_run(cmd, *args, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "bizniz.lib.container_rebuild.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "bizniz.lib.container_rebuild._is_container_running",
            return_value=True,
        ):
            result = maybe_rebuild(
                compose_path="/c.yml",
                service_name="backend",
                workspace_root=tmp_path,
                before_hashes=before,
                health_timeout_s=0.5,
            )
        assert result.triggered
        assert result.mode == "hard"

    def test_image_only_when_container_not_running(self, tmp_path):
        """2026-05-20 hotfix: at IMPLEMENT time before Smoke, container
        isn't up. Rebuild the image (not the container) so deps land
        when Smoke later does `docker compose up`."""
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        before = hash_trigger_files(tmp_path)
        (tmp_path / "requirements.txt").write_text("fastapi\npyjwt\n")

        def fake_run(cmd, *args, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "bizniz.lib.container_rebuild.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "bizniz.lib.container_rebuild._is_container_running",
            return_value=False,  # container NOT running
        ):
            result = maybe_rebuild(
                compose_path="/c.yml",
                service_name="backend",
                workspace_root=tmp_path,
                before_hashes=before,
            )
        assert result.triggered
        assert result.mode == "image_only"
        assert result.success is True


# ── Failure surfacing ──────────────────────────────────────────────


class TestErrorSurfacing:
    def test_pip_install_failure_returns_error_tail(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        before = hash_trigger_files(tmp_path)
        (tmp_path / "requirements.txt").write_text(
            "fastapi\nNONEXISTENT_PACKAGE_NAME\n"
        )

        call_count = [0]
        def fake_run(cmd, *args, **kwargs):
            call_count[0] += 1
            # First call (pip install) fails; subsequent succeed.
            if call_count[0] == 1:
                return MagicMock(
                    returncode=1, stdout="",
                    stderr="ERROR: No matching distribution found",
                )
            return MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "bizniz.lib.container_rebuild.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "bizniz.lib.container_rebuild._is_container_running",
            return_value=True,
        ):
            result = maybe_rebuild(
                compose_path="/c.yml",
                service_name="backend",
                workspace_root=tmp_path,
                before_hashes=before,
                health_timeout_s=0.5,
            )
        assert result.triggered
        assert result.success is False
        assert "No matching distribution" in result.error_tail
