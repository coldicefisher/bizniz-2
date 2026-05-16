"""Tests for ProjectGit — per-project git ops baked into the v2
pipeline (roadmap item 3).

Exercises the wrapper against real ``git`` (system-installed). Each
test creates a fresh tmp_path so there's no cross-test pollution.
All ops are best-effort — assertions cover both happy paths AND the
degraded paths (missing git, bad cwd, etc.) where the wrapper
should return False rather than raise.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bizniz.driver.project_git import ProjectGit


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(
    not _git_available(), reason="git binary not on PATH",
)


def _run_git(cwd: Path, *args) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    )
    return out.stdout.strip()


class TestInit:
    def test_init_creates_repo(self, tmp_path):
        pg = ProjectGit(tmp_path)
        assert pg.init_if_needed() is True
        assert (tmp_path / ".git").is_dir()

    def test_init_writes_gitignore(self, tmp_path):
        pg = ProjectGit(tmp_path)
        pg.init_if_needed()
        gi = tmp_path / ".gitignore"
        assert gi.is_file()
        content = gi.read_text()
        # Spot-check that the template excludes the right things.
        assert "node_modules/" in content
        assert "__pycache__/" in content
        assert "screenshots/" in content
        assert "*.pyc" in content

    def test_init_preserves_existing_gitignore(self, tmp_path):
        custom = "# Custom by user\nnode_modules/\n"
        (tmp_path / ".gitignore").write_text(custom)
        pg = ProjectGit(tmp_path)
        pg.init_if_needed()
        # Init should NOT overwrite an existing .gitignore.
        assert (tmp_path / ".gitignore").read_text() == custom

    def test_init_is_idempotent(self, tmp_path):
        pg = ProjectGit(tmp_path)
        assert pg.init_if_needed() is True
        # Second call should detect existing .git and no-op.
        assert pg.init_if_needed() is False

    def test_init_configures_local_user(self, tmp_path):
        pg = ProjectGit(
            tmp_path,
            user_name="Test User",
            user_email="test@example.com",
        )
        pg.init_if_needed()
        # git config --local should reflect what we passed in.
        name = _run_git(tmp_path, "config", "--local", "user.name")
        email = _run_git(tmp_path, "config", "--local", "user.email")
        assert name == "Test User"
        assert email == "test@example.com"

    def test_init_branch_is_main(self, tmp_path):
        pg = ProjectGit(tmp_path)
        pg.init_if_needed()
        branch = _run_git(tmp_path, "symbolic-ref", "--short", "HEAD")
        assert branch == "main"

    def test_init_skips_when_git_missing(self, tmp_path):
        with patch(
            "bizniz.driver.project_git.shutil.which", return_value=None,
        ):
            pg = ProjectGit(tmp_path)
            assert pg.init_if_needed() is False
            assert not (tmp_path / ".git").exists()

    def test_init_skips_when_project_root_missing(self, tmp_path):
        bogus = tmp_path / "does" / "not" / "exist"
        pg = ProjectGit(bogus)
        assert pg.init_if_needed() is False


class TestCommit:
    def _init_with_file(self, tmp_path) -> ProjectGit:
        pg = ProjectGit(tmp_path)
        pg.init_if_needed()
        (tmp_path / "hello.txt").write_text("hello\n")
        return pg

    def test_commit_with_changes(self, tmp_path):
        pg = self._init_with_file(tmp_path)
        assert pg.commit_all("initial", tag="m0") is True
        # Tag exists.
        tags = _run_git(tmp_path, "tag")
        assert "m0" in tags

    def test_commit_no_changes_returns_false(self, tmp_path):
        pg = self._init_with_file(tmp_path)
        pg.commit_all("initial", tag="m0")
        # Second call with no new changes — nothing to commit.
        assert pg.commit_all("again") is False

    def test_commit_clean_still_applies_tag(self, tmp_path):
        pg = self._init_with_file(tmp_path)
        pg.commit_all("initial", tag="m0")
        # No new changes — but we ask for a new tag at HEAD.
        result = pg.commit_all("clean tag", tag="rerun-tag")
        assert result is False
        tags = _run_git(tmp_path, "tag")
        assert "rerun-tag" in tags

    def test_tag_overwrites_existing(self, tmp_path):
        pg = self._init_with_file(tmp_path)
        pg.commit_all("first commit", tag="snapshot")
        (tmp_path / "second.txt").write_text("two\n")
        pg.commit_all("second commit", tag="snapshot")
        # snapshot should now point to the SECOND commit (force-overwrite).
        log = _run_git(tmp_path, "log", "--oneline", "snapshot")
        first_line = log.splitlines()[0]
        assert "second commit" in first_line

    def test_commit_message_in_log(self, tmp_path):
        pg = self._init_with_file(tmp_path)
        pg.commit_all("my-distinctive-message", tag=None)
        log = _run_git(tmp_path, "log", "--oneline")
        assert "my-distinctive-message" in log


class TestIsDirty:
    def test_clean_after_commit(self, tmp_path):
        pg = ProjectGit(tmp_path)
        pg.init_if_needed()
        (tmp_path / "f.txt").write_text("a")
        pg.commit_all("x")
        assert pg.is_dirty() is False

    def test_dirty_after_new_file(self, tmp_path):
        pg = ProjectGit(tmp_path)
        pg.init_if_needed()
        (tmp_path / "f.txt").write_text("a")
        # Not committed yet — workspace dirty.
        assert pg.is_dirty() is True


class TestRevert:
    def test_revert_to_tag(self, tmp_path):
        pg = ProjectGit(tmp_path)
        pg.init_if_needed()
        (tmp_path / "file_a.txt").write_text("initial state\n")
        pg.commit_all("initial", tag="baseline")
        # Make a "bad" change.
        (tmp_path / "file_a.txt").write_text("BREAKING CHANGE\n")
        (tmp_path / "file_b.txt").write_text("extra junk\n")
        pg.commit_all("bad refactor", tag="latest")
        # Revert.
        assert pg.revert_to_tag("baseline") is True
        # file_a is back to initial, file_b is gone.
        assert (tmp_path / "file_a.txt").read_text() == "initial state\n"
        assert not (tmp_path / "file_b.txt").exists()


class TestCurrentHeadSha:
    def test_returns_sha_after_commit(self, tmp_path):
        pg = ProjectGit(tmp_path)
        pg.init_if_needed()
        (tmp_path / "f.txt").write_text("x")
        pg.commit_all("c1")
        sha = pg.current_head_sha()
        assert sha is not None
        assert len(sha) == 40  # full SHA

    def test_returns_none_before_first_commit(self, tmp_path):
        pg = ProjectGit(tmp_path)
        pg.init_if_needed()
        # No commits yet — HEAD is unborn.
        assert pg.current_head_sha() is None
