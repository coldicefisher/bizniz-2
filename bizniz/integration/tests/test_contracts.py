"""Tests for OpenAPI capture between layers.

Real backends are mocked at the subprocess + urllib boundary so we
don't need a docker daemon. The shape we lock in: capture_backend_
contracts spins up only the named backends, polls /openapi.json on
each, writes <project_root>/contracts/<svc>.openapi.json, and stops
them on exit — even when capture errors mid-flight.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.integration.contracts import (
    capture_backend_contracts,
    load_contract,
)


def _arch_two_backends() -> SystemArchitecture:
    return SystemArchitecture(
        project_name="x", project_slug="x", description="x",
        services=[
            ServiceDefinition(
                name="api1", service_type="backend", framework="fastapi",
                language="python", description="d", workspace_name="api1",
                port=8000,
            ),
            ServiceDefinition(
                name="api2", service_type="backend", framework="express",
                language="typescript", description="d", workspace_name="api2",
                port=8001,
            ),
            ServiceDefinition(
                name="db", service_type="database", framework="postgres",
                language="sql", description="d", workspace_name="db",
                port=5432,
            ),
        ],
    )


def test_captures_only_app_backends_with_ports(tmp_path):
    arch = _arch_two_backends()
    fake_doc = {"paths": {"/x": {"get": {}}}}

    with patch(
        "bizniz.integration.contracts.subprocess.run"
    ) as mock_run, patch(
        "bizniz.integration.contracts._wait_for_openapi", return_value=fake_doc,
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        captured = capture_backend_contracts(
            architecture=arch, project_root=tmp_path,
            compose_path="/fake/compose.yml",
        )

    assert set(captured.keys()) == {"api1", "api2"}  # db filtered out
    # Files written
    assert (tmp_path / "contracts" / "api1.openapi.json").is_file()
    assert (tmp_path / "contracts" / "api2.openapi.json").is_file()


def test_only_names_filter(tmp_path):
    arch = _arch_two_backends()
    fake_doc = {"paths": {"/x": {"get": {}}}}

    with patch(
        "bizniz.integration.contracts.subprocess.run"
    ) as mock_run, patch(
        "bizniz.integration.contracts._wait_for_openapi", return_value=fake_doc,
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        captured = capture_backend_contracts(
            architecture=arch, project_root=tmp_path,
            compose_path="/fake/compose.yml",
            only_names=["api1"],
        )

    assert set(captured.keys()) == {"api1"}


def test_unreachable_backend_omitted_silently(tmp_path):
    arch = _arch_two_backends()

    with patch(
        "bizniz.integration.contracts.subprocess.run"
    ) as mock_run, patch(
        "bizniz.integration.contracts._wait_for_openapi", return_value=None,
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        captured = capture_backend_contracts(
            architecture=arch, project_root=tmp_path,
            compose_path="/fake/compose.yml",
        )

    assert captured == {}  # neither responded → empty
    # No files written for failed captures
    assert not (tmp_path / "contracts" / "api1.openapi.json").exists()


def test_compose_up_failure_returns_empty(tmp_path):
    arch = _arch_two_backends()

    with patch(
        "bizniz.integration.contracts.subprocess.run"
    ) as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "boom"

        captured = capture_backend_contracts(
            architecture=arch, project_root=tmp_path,
            compose_path="/fake/compose.yml",
        )

    assert captured == {}


def test_load_contract_roundtrip(tmp_path):
    """capture writes; load reads — round-trip stability."""
    arch = _arch_two_backends()
    fake_doc = {"openapi": "3.0.0", "paths": {"/foo": {"get": {"summary": "bar"}}}}

    with patch(
        "bizniz.integration.contracts.subprocess.run"
    ) as mock_run, patch(
        "bizniz.integration.contracts._wait_for_openapi", return_value=fake_doc,
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        capture_backend_contracts(
            architecture=arch, project_root=tmp_path,
            compose_path="/fake/compose.yml", only_names=["api1"],
        )

    loaded = load_contract(tmp_path, "api1")
    assert loaded is not None
    assert loaded["paths"]["/foo"]["get"]["summary"] == "bar"


def test_stop_called_in_finally_even_on_capture_failure(tmp_path):
    """Ensure the backend stop step still runs when openapi capture
    raises, so we don't leave dangling containers."""
    arch = _arch_two_backends()

    with patch(
        "bizniz.integration.contracts.subprocess.run"
    ) as mock_run, patch(
        "bizniz.integration.contracts._wait_for_openapi",
        side_effect=RuntimeError("capture exploded"),
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        with pytest.raises(RuntimeError):
            capture_backend_contracts(
                architecture=arch, project_root=tmp_path,
                compose_path="/fake/compose.yml",
            )

    # Last subprocess call should be a `stop` invocation
    last_call_args = mock_run.call_args_list[-1].args[0]
    assert "stop" in last_call_args
