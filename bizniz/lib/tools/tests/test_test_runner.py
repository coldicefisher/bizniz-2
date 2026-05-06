"""Tests for test_runner tool factories."""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.lib.tools.test_runner import (
    PYTEST_SIDECAR_IMAGE,
    build_test_handlers,
    make_run_tests,
    make_smoke_import,
)


def _proc(stdout="", stderr="", returncode=0):
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.returncode = returncode
    return p


# ── run_tests ──────────────────────────────────────────────────────────


class TestRunTests:
    def test_no_compose(self, tmp_path):
        handler = make_run_tests("", tmp_path, "backend")
        assert "ERROR: run_tests unavailable" in handler({})

    def test_default_path(self, tmp_path):
        handler = make_run_tests(
            compose_path="/p/proj/compose.yml",
            workspace_path=tmp_path,
            target_service="backend",
            base_url="http://backend:8000",
        )
        with patch("subprocess.run", return_value=_proc(stdout="passed", returncode=0)) as m:
            out = handler({})
        argv = m.call_args[0][0]
        assert argv[0] == "docker" and argv[1] == "run"
        assert PYTEST_SIDECAR_IMAGE in argv
        assert "--network" in argv
        # network = projectname_app-network where projectname is parent dir lowercased
        i = argv.index("--network")
        assert argv[i + 1] == "proj_app-network"
        # bind mount
        assert f"{tmp_path}:/workspace" in argv
        # pytest cmd contains tests/ default and base_url
        sh_cmd = argv[-1]
        assert "pytest tests/" in sh_cmd
        assert "API_BASE_URL=http://backend:8000" in sh_cmd
        assert "TESTS PASSED" in out

    def test_custom_path(self, tmp_path):
        handler = make_run_tests(
            compose_path="/p/proj/compose.yml",
            workspace_path=tmp_path,
            target_service="backend",
        )
        with patch("subprocess.run", return_value=_proc(stdout="x", returncode=0)) as m:
            handler({"path": "tests/integration/"})
        sh_cmd = m.call_args[0][0][-1]
        assert "pytest tests/integration/" in sh_cmd

    def test_rejects_absolute_path(self, tmp_path):
        handler = make_run_tests("/p/c.yml", tmp_path, "backend")
        out = handler({"path": "/etc/passwd"})
        assert "must be relative" in out

    def test_rejects_dotdot_path(self, tmp_path):
        handler = make_run_tests("/p/c.yml", tmp_path, "backend")
        out = handler({"path": "../../etc"})
        assert "must be relative" in out

    def test_failure_returncode(self, tmp_path):
        handler = make_run_tests("/p/proj/compose.yml", tmp_path, "backend")
        with patch("subprocess.run", return_value=_proc(stdout="failures", returncode=1)):
            out = handler({})
        assert "TESTS FAILED" in out
        assert "exit code: 1" in out

    def test_timeout(self, tmp_path):
        handler = make_run_tests("/p/proj/compose.yml", tmp_path, "backend", timeout_s=5.0)
        err = subprocess.TimeoutExpired("cmd", 5)
        err.stdout = "partial"
        err.stderr = ""
        with patch("subprocess.run", side_effect=err):
            out = handler({})
        assert "timed out" in out
        assert "partial" in out


# ── smoke_import ───────────────────────────────────────────────────────


class TestSmokeImport:
    def test_no_path(self):
        handler = make_smoke_import("/p/c.yml", "backend")
        assert "requires a module" in handler({})

    def test_dotted_path_passed_through(self):
        handler = make_smoke_import("/p/c.yml", "backend")
        with patch("subprocess.run", return_value=_proc(stdout="OK app.api.users /x.py", returncode=0)) as m:
            out = handler({"path": "app.api.users"})
        argv = m.call_args[0][0]
        py_code = argv[-1]
        assert "'app.api.users'" in py_code
        assert "IMPORT OK" in out

    def test_file_path_converted_to_module(self):
        handler = make_smoke_import("/p/c.yml", "backend")
        with patch("subprocess.run", return_value=_proc(stdout="OK", returncode=0)) as m:
            handler({"path": "app/api/users.py"})
        py_code = m.call_args[0][0][-1]
        assert "'app.api.users'" in py_code

    def test_failed_import(self):
        handler = make_smoke_import("/p/c.yml", "backend")
        with patch("subprocess.run", return_value=_proc(stderr="ImportError: foo", returncode=1)):
            out = handler({"path": "app.bad"})
        assert "IMPORT FAILED" in out
        assert "ImportError" in out

    def test_service_override(self):
        handler = make_smoke_import("/p/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="OK", returncode=0)) as m:
            handler({"service": "auth", "path": "app.x"})
        argv = m.call_args[0][0]
        assert "auth" in argv

    def test_timeout(self):
        handler = make_smoke_import("/p/c.yml", "backend")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            out = handler({"path": "app.x"})
        assert "timed out" in out


# ── builder ────────────────────────────────────────────────────────────


class TestBuilder:
    def test_includes_both(self, tmp_path):
        handlers = build_test_handlers(
            compose_path="/p/c.yml",
            workspace_path=tmp_path,
            target_service="backend",
        )
        assert set(handlers.keys()) == {"run_tests", "smoke_import"}
