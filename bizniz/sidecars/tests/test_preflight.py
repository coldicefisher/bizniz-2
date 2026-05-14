"""Tests for sidecar preflight gating."""
from unittest.mock import patch, MagicMock

import pytest

from bizniz.sidecars import (
    REQUIRED_SIDECARS,
    SidecarSpec,
    SidecarPreflightError,
    ensure_sidecars_built,
    image_exists,
    docker_available,
)


def test_required_sidecars_listed():
    """The registry should contain at least the documenters and
    test runners we use today. Names tested loosely so adding a
    new sidecar doesn't break this."""
    images = {s.image for s in REQUIRED_SIDECARS}
    assert "bizniz-doc-typescript:latest" in images
    assert "bizniz-doc-python:latest" in images
    assert "bizniz-test-pytest:latest" in images
    assert "bizniz-test-playwright:latest" in images


def test_each_spec_has_dockerfile_path():
    for spec in REQUIRED_SIDECARS:
        assert spec.dockerfile.name.startswith("Dockerfile.")
        assert spec.image.endswith(":latest")
        assert spec.purpose, f"{spec.image} missing purpose"


@patch("bizniz.sidecars.subprocess.run")
def test_ensure_skips_when_all_built(mock_run):
    """When every image already exists, no docker build is invoked."""
    # docker info → ok; docker image inspect → ok for every image
    mock_run.return_value = MagicMock(returncode=0)
    statuses = []
    ensure_sidecars_built(on_status=statuses.append)
    # All builds skipped: only docker info + image inspects, no builds
    build_calls = [
        c for c in mock_run.call_args_list
        if "build" in (c[0][0] if c[0] else [])
    ]
    assert build_calls == []
    # Status should announce success
    assert any("already built" in s for s in statuses)


@patch("bizniz.sidecars.subprocess.run")
def test_ensure_builds_missing(mock_run):
    """Missing images trigger builds; success returns cleanly."""
    def side_effect(args, **kw):
        if args[:2] == ["docker", "info"]:
            return MagicMock(returncode=0)
        if args[:3] == ["docker", "image", "inspect"]:
            return MagicMock(returncode=1)  # all missing
        if args[:2] == ["docker", "build"]:
            return MagicMock(returncode=0, stderr="")
        return MagicMock(returncode=0)

    mock_run.side_effect = side_effect
    statuses = []
    ensure_sidecars_built(on_status=statuses.append)
    build_calls = [c for c in mock_run.call_args_list if c[0][0][:2] == ["docker", "build"]]
    assert len(build_calls) == len(REQUIRED_SIDECARS)
    assert any("ready" in s.lower() for s in statuses)


@patch("bizniz.sidecars.subprocess.run")
def test_ensure_raises_on_build_failure(mock_run):
    """Failed build aborts preflight."""
    def side_effect(args, **kw):
        if args[:2] == ["docker", "info"]:
            return MagicMock(returncode=0)
        if args[:3] == ["docker", "image", "inspect"]:
            return MagicMock(returncode=1)
        if args[:2] == ["docker", "build"]:
            return MagicMock(returncode=1, stderr="something exploded")
        return MagicMock(returncode=0)

    mock_run.side_effect = side_effect
    with pytest.raises(SidecarPreflightError) as exc:
        ensure_sidecars_built()
    assert "exploded" in str(exc.value)


@patch("bizniz.sidecars.subprocess.run")
def test_ensure_raises_when_docker_unreachable(mock_run):
    """No docker daemon → fail loudly, don't try to build."""
    mock_run.return_value = MagicMock(returncode=1)
    with pytest.raises(SidecarPreflightError) as exc:
        ensure_sidecars_built()
    assert "docker" in str(exc.value).lower()
