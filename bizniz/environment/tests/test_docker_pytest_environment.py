"""
Unit tests for DockerPytestEnvironment.

All Docker subprocess calls are mocked — these tests do NOT require Docker.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
from bizniz.environment.types import ExecutionCallSpec


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_env(tmp_path, **kwargs):
    defaults = dict(
        workspace_root=tmp_path,
        image="myservice:latest",
    )
    defaults.update(kwargs)
    return DockerPytestEnvironment(**defaults)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _container_start_side_effects(container_id="container123"):
    """Side effects for _ensure_container: cleanup, docker run, mkdir, docker cp."""
    return [
        _completed(stdout=""),  # docker ps -a (stale container cleanup)
        _completed(returncode=0, stdout=container_id),  # docker run -d --rm
        _completed(),  # docker exec mkdir -p /workspace
        _completed(),  # docker cp (initial sync)
    ]


def _execute_side_effects(container_id="container123", test_result=None):
    """Side effects for a full execute() call (first call, container not started)."""
    if test_result is None:
        test_result = _completed(stdout="all passed")
    return [
        *_container_start_side_effects(container_id),
        _completed(),  # docker cp (pre-test sync)
        test_result,   # docker exec pytest
    ]


# ── execute() ─────────────────────────────────────────────────────────────────


class TestExecuteCommand:
    """execute() constructs the correct docker exec command."""

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_correct_docker_exec_command(self, mock_run, tmp_path):
        mock_run.side_effect = _execute_side_effects()
        env = _make_env(tmp_path)

        test_file = tmp_path / "tests" / "test_foo.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.touch()

        spec = ExecutionCallSpec(symbol="pytest", args=[str(test_file)])
        env.execute(code="", call_spec=spec)

        # Second call starts the container (first is stale cleanup)
        start_cmd = mock_run.call_args_list[1][0][0]
        assert "docker" in start_cmd
        assert "run" in start_cmd
        assert "-d" in start_cmd
        assert "-v" not in start_cmd  # No bind mount — uses docker cp
        assert "sleep" in start_cmd

        # docker cp is used to sync workspace
        cp_cmd = mock_run.call_args_list[3][0][0]
        assert "docker" in cp_cmd
        assert "cp" in cp_cmd

        # Last call runs pytest via exec
        exec_cmd = mock_run.call_args_list[-1][0][0]
        assert "docker" in exec_cmd
        assert "exec" in exec_cmd
        assert "python3" in exec_cmd
        assert "pytest" in exec_cmd

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_container_reused_on_second_call(self, mock_run, tmp_path):
        mock_run.side_effect = [
            *_execute_side_effects(),            # first execute
            _completed(returncode=0, stdout="true"),  # docker inspect (is running?)
            _completed(),                         # docker cp (pre-test sync)
            _completed(stdout="pass 2"),          # docker exec pytest (2nd)
        ]
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="pytest", args=["test_a.py"])

        env.execute(code="", call_spec=spec)
        env.execute(code="", call_spec=spec)

        # Should NOT start a second container
        cmds = [c[0][0] for c in mock_run.call_args_list]
        run_d_count = sum(1 for c in cmds if "run" in c and "-d" in c)
        assert run_d_count == 1

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_network_enabled_by_default(self, mock_run, tmp_path):
        mock_run.side_effect = _execute_side_effects()
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="pytest", args=["test_a.py"])
        env.execute(code="", call_spec=spec)

        start_cmd = mock_run.call_args_list[1][0][0]
        assert "--network" not in start_cmd

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_network_disabled(self, mock_run, tmp_path):
        mock_run.side_effect = _execute_side_effects()
        env = _make_env(tmp_path, network_enabled=False)
        spec = ExecutionCallSpec(symbol="pytest", args=["test_a.py"])
        env.execute(code="", call_spec=spec)

        start_cmd = mock_run.call_args_list[1][0][0]
        assert "--network" in start_cmd
        net_idx = start_cmd.index("--network")
        assert start_cmd[net_idx + 1] == "none"


class TestExecutePathConversion:
    """execute() converts host paths to /workspace/<relative> container paths."""

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_absolute_path_converted(self, mock_run, tmp_path):
        mock_run.side_effect = _execute_side_effects()
        env = _make_env(tmp_path)

        abs_path = str(tmp_path / "tests" / "test_foo.py")
        spec = ExecutionCallSpec(symbol="pytest", args=[abs_path])
        env.execute(code="", call_spec=spec)

        exec_cmd = mock_run.call_args_list[-1][0][0]
        assert "/workspace/tests/test_foo.py" in exec_cmd

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_relative_path_used_as_is(self, mock_run, tmp_path):
        mock_run.side_effect = _execute_side_effects()
        env = _make_env(tmp_path)

        spec = ExecutionCallSpec(symbol="pytest", args=["tests/test_bar.py"])
        env.execute(code="", call_spec=spec)

        exec_cmd = mock_run.call_args_list[-1][0][0]
        found = [c for c in exec_cmd if "test_bar.py" in c]
        assert len(found) == 1

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_pytest_flags_not_treated_as_paths(self, mock_run, tmp_path):
        mock_run.side_effect = _execute_side_effects()
        env = _make_env(tmp_path)

        spec = ExecutionCallSpec(
            symbol="pytest", args=["test_a.py", "-x", "--maxfail=2"],
        )
        env.execute(code="", call_spec=spec)

        exec_cmd = mock_run.call_args_list[-1][0][0]
        workspace_args = [c for c in exec_cmd if c.startswith("/workspace/")]
        assert len(workspace_args) == 1


class TestExecuteResults:
    """execute() returns correct results based on process exit code."""

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_success_on_zero_exit(self, mock_run, tmp_path):
        mock_run.side_effect = _execute_side_effects(
            test_result=_completed(returncode=0, stdout="2 passed", stderr=""),
        )
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="pytest", args=["test_a.py"])

        result = env.execute(code="", call_spec=spec)

        assert result.success is True
        assert result.result == "2 passed"
        assert result.stdout == "2 passed"

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_failure_on_nonzero_exit(self, mock_run, tmp_path):
        mock_run.side_effect = _execute_side_effects(
            test_result=_completed(returncode=1, stdout="FAILED test_a.py::test_x", stderr=""),
        )
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="pytest", args=["test_a.py"])

        result = env.execute(code="", call_spec=spec)

        assert result.success is False
        assert result.error is not None
        assert result.error.type == "TestFailure"
        assert result.error.stage == "test_execution"
        assert "1" in result.error.message
        assert result.stdout == "FAILED test_a.py::test_x"

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_timeout_handling(self, mock_run, tmp_path):
        mock_run.side_effect = [
            *_container_start_side_effects(),
            _completed(),  # docker cp (pre-test sync)
            subprocess.TimeoutExpired(cmd="docker", timeout=10, output="partial", stderr="err"),
        ]
        env = _make_env(tmp_path, timeout=10)
        spec = ExecutionCallSpec(symbol="pytest", args=["test_a.py"])

        result = env.execute(code="", call_spec=spec)

        assert result.success is False
        assert result.error.type == "TimeoutError"
        assert result.error.stage == "timeout"
        assert "10" in result.error.message

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_container_start_failure(self, mock_run, tmp_path):
        mock_run.return_value = _completed(returncode=1, stderr="image not found")
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="pytest", args=["test_a.py"])

        result = env.execute(code="", call_spec=spec)

        assert result.success is False
        assert result.error.stage == "container_start"

    def test_missing_args(self, tmp_path):
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="pytest", args=[])

        result = env.execute(code="", call_spec=spec)

        assert result.success is False
        assert result.error.type == "ConfigurationError"

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_dict_call_spec_converted(self, mock_run, tmp_path):
        mock_run.side_effect = _execute_side_effects()
        env = _make_env(tmp_path)

        result = env.execute(
            code="",
            call_spec={"symbol": "pytest", "args": ["test_a.py"]},
        )

        assert mock_run.called

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_extra_pytest_args_appended(self, mock_run, tmp_path):
        mock_run.side_effect = _execute_side_effects()
        env = _make_env(tmp_path, extra_pytest_args=["-x", "--maxfail=3"])

        spec = ExecutionCallSpec(symbol="pytest", args=["test_a.py"])
        env.execute(code="", call_spec=spec)

        exec_cmd = mock_run.call_args_list[-1][0][0]
        assert "-x" in exec_cmd
        assert "--maxfail=3" in exec_cmd


# ── install_packages() ────────────────────────────────────────────────────────


class TestInstallPackages:

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_install_runs_pip_in_container(self, mock_run, tmp_path):
        mock_run.side_effect = [
            *_container_start_side_effects(),
            _completed(returncode=0, stdout="Successfully installed fastapi"),  # docker exec pip
            _completed(returncode=0),  # docker commit
        ]
        env = _make_env(tmp_path)
        env.install_packages(["fastapi"])

        # Find the pip install call
        pip_cmds = [c[0][0] for c in mock_run.call_args_list if "pip" in c[0][0]]
        assert len(pip_cmds) == 1
        pip_cmd = pip_cmds[0]
        assert "exec" in pip_cmd
        assert "install" in pip_cmd
        assert "fastapi" in pip_cmd

        # requirements.txt updated
        req_path = tmp_path / "requirements.txt"
        assert req_path.exists()
        assert "fastapi" in req_path.read_text()

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_skips_already_installed(self, mock_run, tmp_path):
        env = _make_env(tmp_path)
        env._installed_packages = ["fastapi"]

        env.install_packages(["fastapi"])

        mock_run.assert_not_called()

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_appends_to_existing_requirements(self, mock_run, tmp_path):
        req_path = tmp_path / "requirements.txt"
        req_path.write_text("flask\nrequests\n")

        mock_run.side_effect = [
            *_container_start_side_effects(),
            _completed(returncode=0),  # pip install
            _completed(returncode=0),  # docker commit
        ]
        env = _make_env(tmp_path)
        env.install_packages(["pydantic"])

        content = req_path.read_text()
        assert "flask" in content
        assert "requests" in content
        assert "pydantic" in content

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_does_not_duplicate_in_requirements(self, mock_run, tmp_path):
        req_path = tmp_path / "requirements.txt"
        req_path.write_text("fastapi\n")

        mock_run.side_effect = [
            *_container_start_side_effects(),
            _completed(returncode=0),  # pip install
            _completed(returncode=0),  # docker commit
        ]
        env = _make_env(tmp_path)
        env.install_packages(["fastapi"])

        content = req_path.read_text()
        assert content.count("fastapi") == 1

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_pip_failure_does_not_update_packages(self, mock_run, tmp_path):
        mock_run.side_effect = [
            *_container_start_side_effects(),
            _completed(returncode=1, stderr="ERROR: No matching distribution"),  # pip fail
        ]
        env = _make_env(tmp_path)
        env.install_packages(["nonexistent-pkg-xyz"])

        assert env.image == "myservice:latest"
        assert env._installed_packages == []


# ── stop() ───────────────────────────────────────────────────────────────────


class TestStop:

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_stop_removes_container(self, mock_run, tmp_path):
        mock_run.side_effect = [
            *_container_start_side_effects(),
            _completed(),  # docker cp (sync back from container)
            _completed(),  # docker rm -f
        ]
        env = _make_env(tmp_path)
        env._ensure_container()
        env.stop()

        rm_cmd = mock_run.call_args_list[-1][0][0]
        assert "rm" in rm_cmd
        assert "-f" in rm_cmd
        assert env._container_id is None

    def test_stop_noop_when_not_started(self, tmp_path):
        env = _make_env(tmp_path)
        env.stop()  # Should not raise


# ── rebuild_image() ───────────────────────────────────────────────────────────


class TestRebuildImage:

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_rebuild_runs_docker_build(self, mock_run, tmp_path):
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM python:3.12-slim\n")

        mock_run.return_value = _completed()
        env = _make_env(tmp_path)

        result = env.rebuild_image()

        assert result is True
        cmd = mock_run.call_args_list[0][0][0]
        assert "docker" in cmd
        assert "build" in cmd
        assert "-t" in cmd
        assert "myservice:latest" in cmd

    def test_rebuild_returns_false_if_no_dockerfile(self, tmp_path):
        env = _make_env(tmp_path)
        result = env.rebuild_image()
        assert result is False

    @patch("bizniz.environment.docker_pytest_environment.subprocess.run")
    def test_rebuild_returns_false_on_build_failure(self, mock_run, tmp_path):
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM python:3.12-slim\n")

        mock_run.side_effect = subprocess.CalledProcessError(1, "docker build")
        env = _make_env(tmp_path)

        result = env.rebuild_image()
        assert result is False


# ── describe() ────────────────────────────────────────────────────────────────


class TestDescribe:

    def test_describe_contents(self, tmp_path):
        env = _make_env(tmp_path)
        desc = env.describe()

        assert "DockerPytestEnvironment" in desc
        assert "myservice:latest" in desc
        assert str(tmp_path) in desc
        assert "120s" in desc
        assert "not started" in desc

    def test_describe_with_packages(self, tmp_path):
        env = _make_env(tmp_path)
        env._installed_packages = ["fastapi", "pydantic"]
        desc = env.describe()

        assert "fastapi" in desc
        assert "pydantic" in desc


# ── Properties and init ──────────────────────────────────────────────────────


class TestInit:

    def test_image_property(self, tmp_path):
        env = _make_env(tmp_path, image="custom:v2")
        assert env.image == "custom:v2"

    def test_workspace_root_resolved(self, tmp_path):
        env = _make_env(tmp_path)
        assert env._workspace_root == tmp_path.resolve()

    def test_defaults(self, tmp_path):
        env = _make_env(tmp_path)
        assert env.timeout == 120
        assert env._extra_pytest_args == []
        assert env._network_enabled is True
        assert env._installed_packages == []
        assert env._container_id is None

    def test_name(self, tmp_path):
        env = _make_env(tmp_path)
        assert env.name == "docker-pytest-environment"
