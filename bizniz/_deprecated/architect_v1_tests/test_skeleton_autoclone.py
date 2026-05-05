"""Unit tests for skeleton auto-clone in seed_workspace().

Mocks subprocess so the tests don't actually hit GitHub.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect import skeletons


@pytest.fixture
def isolated_skeletons_dir(tmp_path, monkeypatch):
    """Point BIZNIZ_SKELETONS_DIR at an empty tmp dir."""
    monkeypatch.setenv("BIZNIZ_SKELETONS_DIR", str(tmp_path))
    return tmp_path


def _fake_clone_factory(repo_layout: dict[str, str]):
    """Build a fake subprocess.run that materializes a repo layout on disk.

    repo_layout is {relpath_in_repo: file_contents}.
    """
    def _fake_run(cmd, *args, **kwargs):
        # Expect: ["git", "clone", "--depth", "1", url, target]
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        for rel, contents in repo_layout.items():
            p = target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(contents)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return _fake_run


# ---------- present-already path ----------

def test_seed_workspace_skips_clone_when_skeleton_already_present(
    isolated_skeletons_dir, tmp_path
):
    repo = isolated_skeletons_dir / "bizniz-skeleton-fastapi"
    repo.mkdir()
    (repo / "main.py").write_text("# {project_slug} {service_name}")

    dest = tmp_path / "dest"
    with patch("bizniz.architect.skeletons.subprocess.run") as mock_run:
        copied = skeletons.seed_workspace(
            skeleton_name="fastapi",
            dest=dest,
            project_slug="proj",
            service_name="svc",
        )
    mock_run.assert_not_called()
    assert "main.py" in copied
    assert (dest / "main.py").read_text() == "# proj svc"


# ---------- happy path: missing → clone → seed ----------

def test_seed_workspace_clones_when_missing(isolated_skeletons_dir, tmp_path):
    dest = tmp_path / "dest"
    fake = _fake_clone_factory({"app/main.py": "fastapi {project_slug}"})

    with patch("bizniz.architect.skeletons.subprocess.run", side_effect=fake) as mock_run:
        statuses = []
        copied = skeletons.seed_workspace(
            skeleton_name="fastapi",
            dest=dest,
            project_slug="myproj",
            service_name="api",
            on_status=statuses.append,
        )

    assert mock_run.call_count == 1
    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["git", "clone"]
    assert "bizniz-skeleton-fastapi.git" in cmd[-2]
    assert cmd[-1] == str(isolated_skeletons_dir / "bizniz-skeleton-fastapi")

    assert "app/main.py" in copied
    assert (dest / "app" / "main.py").read_text() == "fastapi myproj"
    assert any("cloning" in s.lower() for s in statuses)
    assert any("successfully" in s.lower() for s in statuses)


def test_teams_skeletons_share_one_repo(isolated_skeletons_dir, tmp_path):
    """teams-backend, teams-consumer, teams-frontend all live in
    bizniz-skeleton-teams — cloning one populates them all."""
    fake = _fake_clone_factory({
        "backend/main.py": "be",
        "consumer/worker.py": "co",
        "frontend-angular/app.ts": "fe",
    })
    with patch("bizniz.architect.skeletons.subprocess.run", side_effect=fake) as mock_run:
        skeletons.seed_workspace(
            skeleton_name="teams-backend",
            dest=tmp_path / "be_dest",
            project_slug="p", service_name="be",
        )
        # Repo is now present — second call must NOT re-clone.
        skeletons.seed_workspace(
            skeleton_name="teams-consumer",
            dest=tmp_path / "co_dest",
            project_slug="p", service_name="co",
        )
        skeletons.seed_workspace(
            skeleton_name="teams-frontend",
            dest=tmp_path / "fe_dest",
            project_slug="p", service_name="fe",
        )

    assert mock_run.call_count == 1
    assert (tmp_path / "be_dest" / "main.py").read_text() == "be"
    assert (tmp_path / "co_dest" / "worker.py").read_text() == "co"
    assert (tmp_path / "fe_dest" / "app.ts").read_text() == "fe"


# ---------- failure paths ----------

def test_clone_failure_raises_filenotfounderror(isolated_skeletons_dir, tmp_path):
    err = subprocess.CalledProcessError(
        returncode=128, cmd=["git", "clone"], stderr="repo not found"
    )
    with patch("bizniz.architect.skeletons.subprocess.run", side_effect=err):
        with pytest.raises(FileNotFoundError, match="auto-clone failed"):
            skeletons.seed_workspace(
                skeleton_name="fastapi",
                dest=tmp_path / "dest",
                project_slug="p", service_name="s",
            )


def test_clone_timeout_raises_filenotfounderror(isolated_skeletons_dir, tmp_path):
    err = subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=120)
    with patch("bizniz.architect.skeletons.subprocess.run", side_effect=err):
        with pytest.raises(FileNotFoundError, match="timed out"):
            skeletons.seed_workspace(
                skeleton_name="react",
                dest=tmp_path / "dest",
                project_slug="p", service_name="s",
            )


def test_git_binary_missing_raises_filenotfounderror(isolated_skeletons_dir, tmp_path):
    with patch("bizniz.architect.skeletons.subprocess.run", side_effect=FileNotFoundError("no git")):
        with pytest.raises(FileNotFoundError, match="git not found"):
            skeletons.seed_workspace(
                skeleton_name="angular",
                dest=tmp_path / "dest",
                project_slug="p", service_name="s",
            )


def test_partial_clone_is_cleaned_up(isolated_skeletons_dir, tmp_path):
    """If clone fails, repo_root must not be left half-populated, since the
    next call would skip cloning because repo_root.exists() but the per-skel
    subpath is missing."""
    repo_root = isolated_skeletons_dir / "bizniz-skeleton-fastapi"

    def fail_after_partial(cmd, *args, **kwargs):
        # Materialize a partial clone, then "fail".
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "garbage").write_text("partial")
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, stderr="boom"
        )

    with patch("bizniz.architect.skeletons.subprocess.run", side_effect=fail_after_partial):
        with pytest.raises(FileNotFoundError):
            skeletons.seed_workspace(
                skeleton_name="fastapi",
                dest=tmp_path / "dest",
                project_slug="p", service_name="s",
            )

    assert not repo_root.exists(), "partial clone should have been cleaned up"


def test_repo_present_but_subpath_missing_raises(isolated_skeletons_dir, tmp_path):
    """Repo dir exists but expected subpath doesn't — surface the layout
    problem rather than silently re-cloning over user data."""
    repo = isolated_skeletons_dir / "bizniz-skeleton-teams"
    repo.mkdir()
    (repo / "README.md").write_text("partial")
    # No backend/ dir — teams-backend's relative_path is bizniz-skeleton-teams/backend.

    with patch("bizniz.architect.skeletons.subprocess.run") as mock_run:
        with pytest.raises(FileNotFoundError, match="subpath missing"):
            skeletons.seed_workspace(
                skeleton_name="teams-backend",
                dest=tmp_path / "dest",
                project_slug="p", service_name="s",
            )
    mock_run.assert_not_called()


def test_unknown_skeleton_returns_empty_list(isolated_skeletons_dir, tmp_path):
    with patch("bizniz.architect.skeletons.subprocess.run") as mock_run:
        copied = skeletons.seed_workspace(
            skeleton_name="not-a-real-skeleton",
            dest=tmp_path / "dest",
            project_slug="p", service_name="s",
        )
    assert copied == []
    mock_run.assert_not_called()


def test_none_skeleton_returns_empty_list(isolated_skeletons_dir, tmp_path):
    with patch("bizniz.architect.skeletons.subprocess.run") as mock_run:
        copied = skeletons.seed_workspace(
            skeleton_name="none",
            dest=tmp_path / "dest",
            project_slug="p", service_name="s",
        )
    assert copied == []
    mock_run.assert_not_called()
