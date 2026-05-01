"""Tests for the integration phase runner.

Mocks docker + the AI tester so we exercise the orchestration logic
without spinning real containers or burning AI tokens. The
contracts we lock in:

  - dispatch happens once per backend service that has a captured
    contract
  - a backend with no contract gets marked integration_failed
  - a backend whose pytest sidecar exits non-zero gets marked
    integration_failed with the output tail in the error
  - infra failures (compose up) leave service_results untouched
  - teardown (`compose down`) is called on every exit path
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bizniz.architect.types import (
    ServiceDefinition,
    ServiceResult,
    SystemArchitecture,
)
from bizniz.integration.runner import run_integration_phase


def _arch() -> SystemArchitecture:
    return SystemArchitecture(
        project_name="x", project_slug="x", description="x",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="d", workspace_name="backend",
                port=8000,
            ),
            ServiceDefinition(
                name="frontend", service_type="frontend", framework="react",
                language="typescript", description="d", workspace_name="frontend",
                port=5173,
            ),
        ],
    )


def _ok_result(name: str) -> ServiceResult:
    return ServiceResult(
        service_name=name, workspace_name=name, success=True,
        issues_total=4, issues_passed=4,
    )


def _fake_workspace(tmp_path: Path) -> MagicMock:
    """Minimal workspace stub matching what the runner uses."""
    ws_root = tmp_path
    ws = MagicMock()
    ws.root = str(ws_root)
    ws.path = lambda rel: ws_root / rel
    return ws


def _make_factory(returned_source: str = "def test_x(): assert True\n"):
    def factory(workspace=None):
        agent = MagicMock()
        agent.generate_test_file.return_value = returned_source
        return agent
    return factory


def test_dispatches_per_backend_and_writes_test_file(tmp_path):
    arch = _arch()
    initial = [_ok_result("backend"), _ok_result("frontend")]
    contracts = {"backend": {"paths": {"/api/v1/services": {"get": {}}}}}
    ws = _fake_workspace(tmp_path)

    with patch(
        "bizniz.integration.runner._docker_available", return_value=True
    ), patch(
        "bizniz.integration.runner.capture_backend_contracts", return_value=contracts
    ), patch(
        "bizniz.integration.runner.subprocess.run"
    ) as mock_run, patch(
        "bizniz.integration.runner._wait_http_ok", return_value=True,
    ), patch(
        "bizniz.integration.runner._run_pytest_in_sidecar", return_value=(True, "ok"),
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        out = run_integration_phase(
            architecture=arch, service_results=initial,
            project_root=tmp_path, problem_statement="users do things",
            compose_path="/fake/compose.yml",
            http_api_tester_factory=_make_factory(),
            service_workspaces={"backend": ws},
        )

    # Test file written into workspace
    written = (tmp_path / "tests" / "integration" / "test_api.py")
    assert written.is_file()
    assert "def test_x" in written.read_text()
    # Service still passing
    by_name = {r.service_name: r for r in out}
    assert by_name["backend"].success is True


def test_no_contract_marks_service_failed(tmp_path):
    """Backend that didn't expose /openapi.json gets failed, not skipped."""
    arch = _arch()
    initial = [_ok_result("backend")]
    ws = _fake_workspace(tmp_path)

    with patch(
        "bizniz.integration.runner._docker_available", return_value=True
    ), patch(
        "bizniz.integration.runner.capture_backend_contracts", return_value={},  # empty
    ), patch(
        "bizniz.integration.runner.subprocess.run"
    ) as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        out = run_integration_phase(
            architecture=arch, service_results=initial,
            project_root=tmp_path, problem_statement="x",
            compose_path="/fake/compose.yml",
            http_api_tester_factory=_make_factory(),
            service_workspaces={"backend": ws},
        )

    assert out[0].success is False
    assert "integration_failed" in (out[0].error or "")
    assert "openapi" in (out[0].error or "").lower()


def test_failing_pytest_marks_service_failed_with_tail(tmp_path):
    arch = _arch()
    initial = [_ok_result("backend")]
    contracts = {"backend": {"paths": {"/x": {"get": {}}}}}
    ws = _fake_workspace(tmp_path)

    with patch(
        "bizniz.integration.runner._docker_available", return_value=True
    ), patch(
        "bizniz.integration.runner.capture_backend_contracts", return_value=contracts,
    ), patch(
        "bizniz.integration.runner.subprocess.run"
    ) as mock_run, patch(
        "bizniz.integration.runner._wait_http_ok", return_value=True,
    ), patch(
        "bizniz.integration.runner._run_pytest_in_sidecar",
        return_value=(False, "FAILED tests/integration/test_api.py::test_x"),
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        out = run_integration_phase(
            architecture=arch, service_results=initial,
            project_root=tmp_path, problem_statement="x",
            compose_path="/fake/compose.yml",
            http_api_tester_factory=_make_factory(),
            service_workspaces={"backend": ws},
        )

    assert out[0].success is False
    assert "integration_failed" in (out[0].error or "")
    assert "test_x" in (out[0].error or "")  # tail surfaced


def test_compose_up_failure_leaves_results_untouched(tmp_path):
    arch = _arch()
    initial = [_ok_result("backend")]
    ws = _fake_workspace(tmp_path)

    with patch(
        "bizniz.integration.runner._docker_available", return_value=True
    ), patch(
        "bizniz.integration.runner.capture_backend_contracts",
        return_value={"backend": {"paths": {}}},
    ), patch(
        "bizniz.integration.runner.subprocess.run"
    ) as mock_run:
        # First call (compose up) fails; subsequent calls (down) succeed
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="boom", stdout=""),
            MagicMock(returncode=0, stderr="", stdout=""),
        ]

        out = run_integration_phase(
            architecture=arch, service_results=initial,
            project_root=tmp_path, problem_statement="x",
            compose_path="/fake/compose.yml",
            http_api_tester_factory=_make_factory(),
            service_workspaces={"backend": ws},
        )

    assert out is initial
    # compose down attempted as cleanup
    assert any("down" in str(c.args) for c in mock_run.call_args_list)


def test_skipped_when_docker_unavailable(tmp_path):
    arch = _arch()
    initial = [_ok_result("backend")]

    with patch(
        "bizniz.integration.runner._docker_available", return_value=False
    ):
        out = run_integration_phase(
            architecture=arch, service_results=initial,
            project_root=tmp_path, problem_statement="x",
            compose_path="/fake/compose.yml",
            http_api_tester_factory=_make_factory(),
            service_workspaces={"backend": _fake_workspace(tmp_path)},
        )

    assert out is initial


def test_skipped_when_no_backends(tmp_path):
    arch = SystemArchitecture(
        project_name="x", project_slug="x", description="x",
        services=[
            ServiceDefinition(
                name="frontend", service_type="frontend", framework="react",
                language="typescript", description="d", workspace_name="frontend",
                port=5173,
            ),
        ],
    )
    initial = [_ok_result("frontend")]

    out = run_integration_phase(
        architecture=arch, service_results=initial,
        project_root=tmp_path, problem_statement="x",
        compose_path="/fake/compose.yml",
        http_api_tester_factory=_make_factory(),
        service_workspaces={"frontend": _fake_workspace(tmp_path)},
    )
    assert out is initial


def test_compose_down_called_on_test_failure(tmp_path):
    """Stack must always be torn down, even when backend test generation
    raises mid-flight."""
    arch = _arch()
    initial = [_ok_result("backend")]
    contracts = {"backend": {"paths": {"/x": {"get": {}}}}}
    ws = _fake_workspace(tmp_path)

    def boom_factory(workspace=None):
        agent = MagicMock()
        agent.generate_test_file.side_effect = RuntimeError("AI down")
        return agent

    with patch(
        "bizniz.integration.runner._docker_available", return_value=True
    ), patch(
        "bizniz.integration.runner.capture_backend_contracts", return_value=contracts,
    ), patch(
        "bizniz.integration.runner.subprocess.run"
    ) as mock_run, patch(
        "bizniz.integration.runner._wait_http_ok", return_value=True,
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        out = run_integration_phase(
            architecture=arch, service_results=initial,
            project_root=tmp_path, problem_statement="x",
            compose_path="/fake/compose.yml",
            http_api_tester_factory=boom_factory,
            service_workspaces={"backend": ws},
        )

    # Service failed gracefully
    assert out[0].success is False
    assert "AI down" in (out[0].error or "")
    # Compose down still ran in finally
    assert any("down" in str(c.args) for c in mock_run.call_args_list)
