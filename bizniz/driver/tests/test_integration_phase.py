"""Tests for driver.integration_phase. Mocks the v1 testers + sidecar runners."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.driver.integration_phase import (
    IntegrationPhase, IntegrationPhaseResult,
)
from bizniz.planner.types import Milestone


def _arch(*services):
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=list(services),
    )


def _backend(name="backend", port=8000):
    return ServiceDefinition(
        name=name, service_type="backend", framework="fastapi",
        language="python", description="API",
        workspace_name=name, port=port,
    )


def _frontend(name="frontend", port=5173):
    return ServiceDefinition(
        name=name, service_type="frontend", framework="react",
        language="typescript", description="UI",
        workspace_name=name, port=port,
    )


def _milestone():
    return Milestone(
        sequence_index=1, name="M1", problem_slice="x",
    )


def _make_workspace(tmp_path, name):
    ws = MagicMock()
    ws.root = tmp_path / name
    ws.write_text = MagicMock()
    return ws


class TestRunApi:
    def test_no_backends_returns_passed(self, tmp_path):
        ip = IntegrationPhase(
            http_tester_factory=MagicMock(),
            web_tester_factory=MagicMock(),
        )
        arch = _arch(_frontend())
        result = ip.run_api(
            milestone=_milestone(), architecture=arch,
            project_root=tmp_path, compose_path="/p/c.yml",
            service_workspaces={"frontend": _make_workspace(tmp_path, "frontend")},
        )
        assert result.passed is True
        assert result.phase == "api"

    def test_passes_when_pytest_passes(self, tmp_path):
        tester = MagicMock()
        tester.generate_test_file.return_value = "def test_x(): assert True"
        factory = MagicMock(return_value=tester)
        ip = IntegrationPhase(
            http_tester_factory=factory,
            web_tester_factory=MagicMock(),
        )
        backend = _backend()
        ws = _make_workspace(tmp_path, "backend")

        with patch("bizniz.driver.integration_phase.capture_backend_contracts") as cap, \
             patch("bizniz.driver.integration_phase._run_pytest_in_sidecar") as pyt:
            cap.return_value = {"backend": {"openapi": "3.0.0"}}
            pyt.return_value = (True, "passed 1 test")
            result = ip.run_api(
                milestone=_milestone(), architecture=_arch(backend),
                project_root=tmp_path, compose_path="/p/c.yml",
                service_workspaces={"backend": ws},
            )
        assert result.passed is True
        assert "backend" in result.backend_contracts
        assert ws.write_text.called

    def test_no_workspace_marks_failed(self, tmp_path):
        ip = IntegrationPhase(
            http_tester_factory=MagicMock(),
            web_tester_factory=MagicMock(),
        )
        with patch("bizniz.driver.integration_phase.capture_backend_contracts") as cap:
            cap.return_value = {"backend": {}}
            result = ip.run_api(
                milestone=_milestone(), architecture=_arch(_backend()),
                project_root=tmp_path, compose_path="/p/c.yml",
                service_workspaces={},  # empty — backend lookup fails
            )
        assert result.passed is False
        assert "no workspace" in (result.error_summary or "")

    def test_failed_pytest_no_debugger_returns_failed(self, tmp_path):
        tester = MagicMock()
        tester.generate_test_file.return_value = "y"
        ip = IntegrationPhase(
            http_tester_factory=MagicMock(return_value=tester),
            web_tester_factory=MagicMock(),
            debugger_factory=None,  # no debugger
        )
        with patch("bizniz.driver.integration_phase.capture_backend_contracts") as cap, \
             patch("bizniz.driver.integration_phase._run_pytest_in_sidecar") as pyt:
            cap.return_value = {"backend": {}}
            pyt.return_value = (False, "ImportError: foo")
            result = ip.run_api(
                milestone=_milestone(), architecture=_arch(_backend()),
                project_root=tmp_path, compose_path="/p/c.yml",
                service_workspaces={"backend": _make_workspace(tmp_path, "backend")},
            )
        assert result.passed is False
        assert "ImportError" in (result.error_summary or "")

    def test_debugger_repairs_then_passes(self, tmp_path):
        tester = MagicMock()
        tester.generate_test_file.return_value = "y"
        debugger = MagicMock()
        debugger.debug = MagicMock()
        ip = IntegrationPhase(
            http_tester_factory=MagicMock(return_value=tester),
            web_tester_factory=MagicMock(),
            debugger_factory=MagicMock(return_value=debugger),
            debugger_max_iterations=3,
        )
        sidecar_results = [(False, "fail"), (True, "passed")]
        with patch("bizniz.driver.integration_phase.capture_backend_contracts") as cap, \
             patch("bizniz.driver.integration_phase._run_pytest_in_sidecar") as pyt:
            cap.return_value = {"backend": {}}
            pyt.side_effect = sidecar_results
            result = ip.run_api(
                milestone=_milestone(), architecture=_arch(_backend()),
                project_root=tmp_path, compose_path="/p/c.yml",
                service_workspaces={"backend": _make_workspace(tmp_path, "backend")},
            )
        assert result.passed is True
        debugger.debug.assert_called_once()

    def test_tester_raises_marks_failed(self, tmp_path):
        tester = MagicMock()
        tester.generate_test_file.side_effect = RuntimeError("tester broke")
        ip = IntegrationPhase(
            http_tester_factory=MagicMock(return_value=tester),
            web_tester_factory=MagicMock(),
        )
        with patch("bizniz.driver.integration_phase.capture_backend_contracts") as cap:
            cap.return_value = {"backend": {}}
            result = ip.run_api(
                milestone=_milestone(), architecture=_arch(_backend()),
                project_root=tmp_path, compose_path="/p/c.yml",
                service_workspaces={"backend": _make_workspace(tmp_path, "backend")},
            )
        assert result.passed is False
        assert "tester broke" in (result.error_summary or "")


class TestRunWeb:
    def test_no_frontends_passes(self, tmp_path):
        ip = IntegrationPhase(
            http_tester_factory=MagicMock(),
            web_tester_factory=MagicMock(),
        )
        result = ip.run_web(
            milestone=_milestone(), architecture=_arch(_backend()),
            project_root=tmp_path, compose_path="/p/c.yml",
            service_workspaces={},
            backend_contracts={},
        )
        assert result.passed is True
        assert result.phase == "web"

    def test_passes_when_playwright_passes(self, tmp_path):
        tester = MagicMock()
        tester.generate_test_file.return_value = "test('x', () => {})"
        ip = IntegrationPhase(
            http_tester_factory=MagicMock(),
            web_tester_factory=MagicMock(return_value=tester),
        )
        ws = _make_workspace(tmp_path, "frontend")
        with patch("bizniz.driver.integration_phase._run_playwright_in_sidecar") as pw:
            pw.return_value = (True, "1 passed")
            result = ip.run_web(
                milestone=_milestone(),
                architecture=_arch(_frontend()),
                project_root=tmp_path, compose_path="/p/c.yml",
                service_workspaces={"frontend": ws},
                backend_contracts={"backend": {}},
            )
        assert result.passed is True
        assert ws.write_text.called

    def test_failed_playwright_marks_failed(self, tmp_path):
        tester = MagicMock()
        tester.generate_test_file.return_value = "y"
        ip = IntegrationPhase(
            http_tester_factory=MagicMock(),
            web_tester_factory=MagicMock(return_value=tester),
        )
        with patch("bizniz.driver.integration_phase._run_playwright_in_sidecar") as pw:
            pw.return_value = (False, "1 failed")
            result = ip.run_web(
                milestone=_milestone(),
                architecture=_arch(_frontend()),
                project_root=tmp_path, compose_path="/p/c.yml",
                service_workspaces={"frontend": _make_workspace(tmp_path, "frontend")},
                backend_contracts={},
            )
        assert result.passed is False
