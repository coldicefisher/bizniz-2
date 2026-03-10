"""
Unit tests for DockerJestEnvironment.

All Docker subprocess calls are mocked — these tests do NOT require Docker.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from bizniz.environment.docker_jest_environment import DockerJestEnvironment
from bizniz.environment.types import ExecutionCallSpec


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_env(tmp_path, **kwargs):
    defaults = dict(
        workspace_root=tmp_path,
        image="myservice:latest",
    )
    defaults.update(kwargs)
    return DockerJestEnvironment(**defaults)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ── execute() ─────────────────────────────────────────────────────────────────


class TestExecuteCommand:
    """execute() constructs the correct docker exec command."""

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_correct_docker_command_with_mount(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),  # docker run -d
            _completed(stdout="Tests: 2 passed"),  # docker exec jest
        ]
        env = _make_env(tmp_path)

        test_file = tmp_path / "tests" / "App.test.tsx"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.touch()

        spec = ExecutionCallSpec(symbol="jest", args=[str(test_file)])
        env.execute(code="", call_spec=spec)

        # First call starts container with volume mount
        start_cmd = mock_run.call_args_list[0][0][0]
        assert "-v" in start_cmd
        vol_idx = start_cmd.index("-v")
        assert start_cmd[vol_idx + 1] == f"{tmp_path}:/workspace"
        assert "myservice:latest" in start_cmd

        # Second call runs jest via exec
        exec_cmd = mock_run.call_args_list[1][0][0]
        assert "exec" in exec_cmd
        assert "npx" in exec_cmd
        assert "jest" in exec_cmd

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_container_reused_on_second_call(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),  # docker run -d
            _completed(stdout="pass 1"),  # docker exec jest (1st)
            _completed(returncode=0, stdout="true"),  # docker inspect
            _completed(stdout="pass 2"),  # docker exec jest (2nd)
        ]
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="jest", args=["test_a.test.ts"])

        env.execute(code="", call_spec=spec)
        env.execute(code="", call_spec=spec)

        cmds = [c[0][0] for c in mock_run.call_args_list]
        run_d_count = sum(1 for c in cmds if "run" in c and "-d" in c)
        assert run_d_count == 1

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_network_disabled_by_default(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),
            _completed(),
        ]
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="jest", args=["test_a.test.ts"])
        env.execute(code="", call_spec=spec)

        start_cmd = mock_run.call_args_list[0][0][0]
        assert "--network" in start_cmd
        net_idx = start_cmd.index("--network")
        assert start_cmd[net_idx + 1] == "none"

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_network_enabled(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),
            _completed(),
        ]
        env = _make_env(tmp_path, network_enabled=True)
        spec = ExecutionCallSpec(symbol="jest", args=["test_a.test.ts"])
        env.execute(code="", call_spec=spec)

        start_cmd = mock_run.call_args_list[0][0][0]
        assert "--network" not in start_cmd


class TestExecutePathConversion:
    """execute() converts host paths to /workspace/<relative> container paths."""

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_absolute_path_converted(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),
            _completed(),
        ]
        env = _make_env(tmp_path)

        abs_path = str(tmp_path / "tests" / "App.test.tsx")
        spec = ExecutionCallSpec(symbol="jest", args=[abs_path])
        env.execute(code="", call_spec=spec)

        exec_cmd = mock_run.call_args_list[1][0][0]
        assert "/workspace/tests/App.test.tsx" in exec_cmd

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_relative_path_used_as_is(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),
            _completed(),
        ]
        env = _make_env(tmp_path)

        spec = ExecutionCallSpec(symbol="jest", args=["tests/App.test.tsx"])
        env.execute(code="", call_spec=spec)

        exec_cmd = mock_run.call_args_list[1][0][0]
        found = [c for c in exec_cmd if "App.test.tsx" in c]
        assert len(found) == 1


class TestExecuteResults:
    """execute() returns correct results based on process exit code."""

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_success_on_zero_exit(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),
            _completed(returncode=0, stdout="Tests: 2 passed", stderr=""),
        ]
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="jest", args=["test_a.test.ts"])

        result = env.execute(code="", call_spec=spec)

        assert result.success is True
        assert "2 passed" in result.result
        assert result.stdout == "Tests: 2 passed"

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_failure_on_nonzero_exit(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),
            _completed(returncode=1, stdout="", stderr="FAIL tests/App.test.tsx"),
        ]
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="jest", args=["test_a.test.ts"])

        result = env.execute(code="", call_spec=spec)

        assert result.success is False
        assert result.error is not None
        assert result.error.type == "TestFailure"
        assert result.error.stage == "test_execution"
        assert "1" in result.error.message

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_stderr_included_in_failure_traceback(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),
            _completed(returncode=1, stdout="", stderr="FAIL src/App.test.tsx\n  TypeError: x is not a function"),
        ]
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="jest", args=["test_a.test.ts"])

        result = env.execute(code="", call_spec=spec)

        assert "TypeError" in result.error.traceback

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_timeout_handling(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),
            subprocess.TimeoutExpired(cmd="docker", timeout=10, output="partial", stderr="err"),
        ]
        env = _make_env(tmp_path, timeout=10)
        spec = ExecutionCallSpec(symbol="jest", args=["test_a.test.ts"])

        result = env.execute(code="", call_spec=spec)

        assert result.success is False
        assert result.error.type == "TimeoutError"
        assert result.error.stage == "timeout"
        assert "10" in result.error.message

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_container_start_failure(self, mock_run, tmp_path):
        mock_run.return_value = _completed(returncode=1, stderr="image not found")
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="jest", args=["test_a.test.ts"])

        result = env.execute(code="", call_spec=spec)

        assert result.success is False
        assert result.error.stage == "container_start"

    def test_missing_args(self, tmp_path):
        env = _make_env(tmp_path)
        spec = ExecutionCallSpec(symbol="jest", args=[])

        result = env.execute(code="", call_spec=spec)

        assert result.success is False
        assert result.error.type == "ConfigurationError"

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_dict_call_spec_converted(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),
            _completed(),
        ]
        env = _make_env(tmp_path)

        result = env.execute(
            code="",
            call_spec={"symbol": "jest", "args": ["test_a.test.ts"]},
        )

        assert mock_run.called

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_extra_jest_args_appended(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),
            _completed(),
        ]
        env = _make_env(tmp_path, extra_jest_args=["--coverage", "--silent"])

        spec = ExecutionCallSpec(symbol="jest", args=["test_a.test.ts"])
        env.execute(code="", call_spec=spec)

        exec_cmd = mock_run.call_args_list[1][0][0]
        assert "--coverage" in exec_cmd
        assert "--silent" in exec_cmd


# ── install_packages() ────────────────────────────────────────────────────────


class TestInstallPackages:

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_install_runs_npm_in_container(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),  # start
            _completed(returncode=0, stdout="added 1 package"),  # docker exec npm
            _completed(returncode=0),  # docker commit
        ]
        env = _make_env(tmp_path)
        env.install_packages(["@testing-library/react"])

        calls = mock_run.call_args_list
        npm_cmd = calls[1][0][0]
        assert "exec" in npm_cmd
        assert "npm" in npm_cmd
        assert "install" in npm_cmd
        assert "@testing-library/react" in npm_cmd

        commit_cmd = calls[2][0][0]
        assert "commit" in commit_cmd

        assert env.image == "myservice:latest"

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_skips_already_installed(self, mock_run, tmp_path):
        env = _make_env(tmp_path)
        env._installed_packages = ["jest"]

        env.install_packages(["jest"])

        mock_run.assert_not_called()

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_npm_failure_does_not_update_packages(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),  # start
            _completed(returncode=1, stderr="ERR! 404 Not Found"),  # npm fail
        ]
        env = _make_env(tmp_path)
        env.install_packages(["nonexistent-pkg-xyz"])

        assert env.image == "myservice:latest"
        assert env._installed_packages == []


# ── stop() ───────────────────────────────────────────────────────────────────


class TestStop:

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_stop_removes_container(self, mock_run, tmp_path):
        mock_run.side_effect = [
            _completed(returncode=0, stdout="container123"),  # start
            _completed(),  # exec (fix permissions)
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

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_rebuild_runs_docker_build(self, mock_run, tmp_path):
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM node:20-slim\n")

        mock_run.return_value = _completed()
        env = _make_env(tmp_path)

        result = env.rebuild_image()

        assert result is True
        cmd = mock_run.call_args_list[0][0][0]
        assert "docker" in cmd
        assert "build" in cmd

    def test_rebuild_returns_false_if_no_dockerfile(self, tmp_path):
        env = _make_env(tmp_path)
        result = env.rebuild_image()
        assert result is False

    @patch("bizniz.environment.docker_jest_environment.subprocess.run")
    def test_rebuild_returns_false_on_build_failure(self, mock_run, tmp_path):
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM node:20-slim\n")

        mock_run.side_effect = subprocess.CalledProcessError(1, "docker build")
        env = _make_env(tmp_path)

        result = env.rebuild_image()
        assert result is False


# ── describe() ────────────────────────────────────────────────────────────────


class TestDescribe:

    def test_describe_contents(self, tmp_path):
        env = _make_env(tmp_path)
        desc = env.describe()

        assert "DockerJestEnvironment" in desc
        assert "myservice:latest" in desc
        assert str(tmp_path) in desc
        assert "120s" in desc
        assert "not started" in desc

    def test_describe_with_packages(self, tmp_path):
        env = _make_env(tmp_path)
        env._installed_packages = ["jest", "@testing-library/react"]
        desc = env.describe()

        assert "jest" in desc
        assert "@testing-library/react" in desc


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
        assert env._extra_jest_args == []
        assert env._network_enabled is False
        assert env._installed_packages == []
        assert env._container_id is None

    def test_name(self, tmp_path):
        env = _make_env(tmp_path)
        assert env.name == "docker-jest-environment"
