"""Unit tests for AI-assisted Docker build recovery.

Mocks the AI client and the rebuild callable.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner import Provisioner
from bizniz.provisioner.ai_recovery import (
    AIRecoveryResponse,
    call_ai_recovery,
    try_ai_recovery,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _ai_response(payload: dict):
    return json.dumps(payload), "job-id", []


def _make_client(payload: dict) -> MagicMock:
    client = MagicMock()
    client.get_text.return_value = _ai_response(payload)
    return client


def _service(**overrides) -> ServiceDefinition:
    base = dict(
        name="backend", service_type="backend", framework="fastapi",
        language="python", description="api",
        workspace_name="backend", port=8000, depends_on=[], requirements=[],
        skeleton="none",
    )
    base.update(overrides)
    return ServiceDefinition(**base)


def _arch(*services, slug="proj") -> SystemArchitecture:
    return SystemArchitecture(
        project_name="Proj", project_slug=slug, description="d",
        services=list(services),
    )


# ── call_ai_recovery ─────────────────────────────────────────────────────────


def test_call_ai_recovery_returns_parsed_response():
    payload = {
        "dockerfile": "FROM python:3.12-slim\nRUN pip install fastapi",
        "explanation": "added missing pip install",
    }
    client = _make_client(payload)
    response = call_ai_recovery(
        client=client,
        dockerfile_text="FROM python:3.12-slim",
        build_error="ImportError: fastapi",
        attempt=1,
        max_retries=2,
    )
    assert response.dockerfile.startswith("FROM python:3.12-slim")
    assert "added missing pip install" in response.explanation


def test_call_ai_recovery_raises_on_empty():
    client = MagicMock()
    client.get_text.return_value = ("", "job", [])
    with pytest.raises(ValueError, match="empty"):
        call_ai_recovery(
            client=client, dockerfile_text="x", build_error="y",
            attempt=1, max_retries=2,
        )


# ── try_ai_recovery (orchestration) ──────────────────────────────────────────


def test_try_ai_recovery_succeeds_on_first_retry(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim")

    client = _make_client({
        "dockerfile": "FROM python:3.12-slim\nRUN pip install fastapi",
        "explanation": "added install",
    })
    rebuild = MagicMock()  # no exception → success

    ok = try_ai_recovery(
        client=client,
        dockerfile_path=dockerfile,
        build_error="ImportError: fastapi",
        rebuild=rebuild,
        max_retries=2,
    )
    assert ok is True
    rebuild.assert_called_once()
    assert "RUN pip install fastapi" in dockerfile.read_text()
    # Backup of original
    assert (tmp_path / "Dockerfile.pre-ai-recovery-1").exists()


def test_try_ai_recovery_succeeds_on_second_attempt(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim")

    client = MagicMock()
    client.get_text.side_effect = [
        _ai_response({"dockerfile": "FROM python:3.12-slim\nRUN echo first",
                      "explanation": "first try"}),
        _ai_response({"dockerfile": "FROM python:3.12-slim\nRUN echo second",
                      "explanation": "second try"}),
    ]
    rebuild = MagicMock(side_effect=[
        RuntimeError("still broken"),  # attempt 1 fails
        None,                          # attempt 2 succeeds
    ])

    ok = try_ai_recovery(
        client=client,
        dockerfile_path=dockerfile,
        build_error="initial err",
        rebuild=rebuild,
        max_retries=2,
    )
    assert ok is True
    assert rebuild.call_count == 2
    assert "second" in dockerfile.read_text()
    assert (tmp_path / "Dockerfile.pre-ai-recovery-1").exists()
    assert (tmp_path / "Dockerfile.pre-ai-recovery-2").exists()


def test_try_ai_recovery_caps_at_max_retries(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim")

    client = MagicMock()
    client.get_text.return_value = _ai_response({
        "dockerfile": "FROM python:3.12-slim\nRUN x",
        "explanation": "no fix",
    })
    rebuild = MagicMock(side_effect=RuntimeError("still broken"))

    ok = try_ai_recovery(
        client=client,
        dockerfile_path=dockerfile,
        build_error="err",
        rebuild=rebuild,
        max_retries=2,
    )
    assert ok is False
    assert rebuild.call_count == 2
    assert client.get_text.call_count == 2


def test_try_ai_recovery_aborts_when_dockerfile_missing(tmp_path):
    client = _make_client({"dockerfile": "x", "explanation": ""})
    rebuild = MagicMock()
    ok = try_ai_recovery(
        client=client,
        dockerfile_path=tmp_path / "nonexistent",
        build_error="err",
        rebuild=rebuild,
        max_retries=2,
    )
    assert ok is False
    rebuild.assert_not_called()


def test_try_ai_recovery_aborts_on_empty_dockerfile_response(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM x")
    client = _make_client({"dockerfile": "", "explanation": ""})
    rebuild = MagicMock()
    ok = try_ai_recovery(
        client=client,
        dockerfile_path=dockerfile,
        build_error="err",
        rebuild=rebuild,
        max_retries=2,
    )
    assert ok is False
    rebuild.assert_not_called()
    # Original Dockerfile preserved
    assert dockerfile.read_text() == "FROM x"


def test_try_ai_recovery_aborts_when_ai_call_raises(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM x")
    client = MagicMock()
    client.get_text.side_effect = RuntimeError("API down")
    rebuild = MagicMock()
    ok = try_ai_recovery(
        client=client, dockerfile_path=dockerfile,
        build_error="err", rebuild=rebuild, max_retries=2,
    )
    assert ok is False
    rebuild.assert_not_called()


# ── Provisioner integration ──────────────────────────────────────────────────


def test_provisioner_recovery_disabled_marks_failed(tmp_path):
    """No AI hooks → original behavior: build fails, service marked failed."""
    arch = _arch(_service(name="backend", workspace_name="backend",
                          port=8001, skeleton="none"))
    p = Provisioner(project_parent=tmp_path, build_images=True)

    with patch("bizniz.provisioner.provisioner.build_image",
               side_effect=RuntimeError("build broken")):
        result = p.provision(arch, project_name="Proj")

    backend = next(s for s in result.services if s.name == "backend")
    assert backend.image_built is False


def test_provisioner_recovery_enabled_runs_ai_loop_on_build_failure(tmp_path):
    arch = _arch(_service(name="backend", workspace_name="backend",
                          port=8001, skeleton="none"))

    client = _make_client({
        "dockerfile": "FROM python:3.12-slim\nRUN pip install fastapi",
        "explanation": "fixed",
    })
    factory = MagicMock(return_value=client)

    # First build_image call raises, second (after AI patch) succeeds.
    build_calls = [RuntimeError("missing fastapi"), None]

    def fake_build(*a, **kw):
        result = build_calls.pop(0)
        if isinstance(result, Exception):
            raise result

    p = Provisioner(
        project_parent=tmp_path, build_images=True,
        ai_client_factory=factory,
        ai_recovery_enabled=True,
        ai_recovery_max_retries=2,
    )
    with patch("bizniz.provisioner.provisioner.build_image", side_effect=fake_build):
        result = p.provision(arch, project_name="Proj")

    backend = next(s for s in result.services if s.name == "backend")
    assert backend.image_built is True
    factory.assert_called_once_with("gemini-pro")
    client.get_text.assert_called_once()

    # Patched Dockerfile written
    dockerfile = tmp_path / "proj" / "infra" / "development" / "backend" / "Dockerfile"
    assert "RUN pip install fastapi" in dockerfile.read_text()


def test_provisioner_recovery_exhausts_retries_then_marks_failed(tmp_path):
    arch = _arch(_service(name="backend", workspace_name="backend",
                          port=8001, skeleton="none"))
    client = _make_client({
        "dockerfile": "FROM python:3.12-slim\nRUN x", "explanation": "no help",
    })
    factory = MagicMock(return_value=client)

    p = Provisioner(
        project_parent=tmp_path, build_images=True,
        ai_client_factory=factory,
        ai_recovery_enabled=True,
        ai_recovery_max_retries=2,
    )
    with patch("bizniz.provisioner.provisioner.build_image",
               side_effect=RuntimeError("permanently broken")):
        result = p.provision(arch, project_name="Proj")

    backend = next(s for s in result.services if s.name == "backend")
    assert backend.image_built is False
    assert client.get_text.call_count == 2  # exactly max_retries calls


def test_provisioner_requires_factory_when_recovery_enabled(tmp_path):
    from bizniz.provisioner.types import ProvisionerError
    with pytest.raises(ProvisionerError, match="ai_client_factory"):
        Provisioner(
            project_parent=tmp_path, build_images=False,
            ai_recovery_enabled=True,
        )
