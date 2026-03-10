"""
DockerPytestEnvironment

Runs pytest inside a persistent Docker container with the workspace mounted,
so that third-party dependencies (e.g. ``fastapi``, ``pydantic``) are available
at import time.

The container is started once (lazily on first execute()) and reused for all
subsequent test runs via ``docker exec``. This eliminates container startup
overhead (~5-10s per run) which compounds across many iterations.

Usage in the orchestrator::

    env = DockerPytestEnvironment(
        workspace_root=workspace.root,
        image="bizniz-service-abc:latest",
    )
    call_spec = ExecutionCallSpec(symbol="pytest", args=["/abs/path/to/test_file.py"])
    result = env.execute(code="", call_spec=call_spec)

    # When done, clean up:
    env.stop()

The ``code`` argument is intentionally unused — the test file is already on disk.
The workspace root is bind-mounted at ``/workspace`` inside the container and
``PYTHONPATH=/workspace`` is set so plain ``import module_name`` works.
"""

import os
import subprocess
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional, List, Union

from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import (
    ExecutionCallSpec,
    ExecutionEnvironmentResult,
    ExecutionEnvironmentErrorDetails,
)


class DockerPytestEnvironment(BaseExecutionEnvironment):
    """
    Runs pytest inside a persistent Docker container with the workspace mounted.

    The container is started lazily on the first execute() call and kept alive
    for all subsequent runs. Use stop() to clean up, or use as a context manager.
    """

    name: str = "docker-pytest-environment"

    def __init__(
        self,
        workspace_root: Union[Path, str],
        image: str,
        timeout: int = 60,
        extra_pytest_args: Optional[List[str]] = None,
        network_enabled: bool = False,
    ):
        super().__init__(timeout=timeout)
        self._workspace_root = Path(workspace_root).resolve()
        self._image = image
        self._extra_pytest_args = extra_pytest_args or []
        self._network_enabled = network_enabled
        self._installed_packages: List[str] = []
        self._container_id: Optional[str] = None
        self._container_name = f"bizniz-pytest-{uuid.uuid4().hex[:12]}"

    @property
    def image(self) -> str:
        return self._image

    # ── Container lifecycle ────────────────────────────────────────────────────

    def _ensure_container(self):
        """Start the persistent container if not already running."""
        if self._container_id is not None:
            # Verify it's still running
            check = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._container_id],
                capture_output=True, text=True,
            )
            if check.returncode == 0 and "true" in check.stdout.strip().lower():
                return
            # Container died, reset and restart
            self._container_id = None

        cmd = [
            "docker", "run", "-d",
            "--name", self._container_name,
            "-v", f"{self._workspace_root}:/workspace",
            "-w", "/workspace",
            "-e", "PYTHONPATH=/workspace",
        ]

        if not self._network_enabled:
            cmd += ["--network", "none"]

        cmd += [self._image, "sleep", "infinity"]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to start persistent container: {proc.stderr.strip()}"
            )
        self._container_id = proc.stdout.strip()

    def stop(self):
        """Stop and remove the persistent container."""
        if self._container_id is not None:
            self._fix_permissions()
            subprocess.run(
                ["docker", "rm", "-f", self._container_id],
                capture_output=True, timeout=10,
            )
            self._container_id = None

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass

    # ── BaseExecutionEnvironment interface ──────────────────────────────────────

    def execute(
        self,
        code: str,
        call_spec: ExecutionCallSpec,
    ) -> ExecutionEnvironmentResult:
        """
        Run pytest inside the persistent Docker container via docker exec.

        call_spec.args should contain test file paths (relative to workspace root).
        These get converted to /workspace/<relative_path> inside the container.
        """
        if isinstance(call_spec, dict):
            call_spec = ExecutionCallSpec(**call_spec)

        if not call_spec.args:
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    type="ConfigurationError",
                    message="call_spec.args[0] must be the path to the test file.",
                ),
            )

        # Ensure persistent container is running
        try:
            self._ensure_container()
        except Exception as e:
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="container_start",
                    type=type(e).__name__,
                    message=str(e),
                ),
            )

        # Convert absolute host paths to container paths
        test_paths_container = []
        for arg in call_spec.args:
            if arg.startswith("-"):
                break
            p = Path(arg).resolve()
            try:
                relative = p.relative_to(self._workspace_root)
            except ValueError:
                relative = Path(arg)
            test_paths_container.append(f"/workspace/{relative}")

        cmd = [
            "docker", "exec",
            self._container_id,
            "python3", "-m", "pytest",
            *test_paths_container,
            "-v", "--tb=long", "--no-header",
            *self._extra_pytest_args,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="timeout",
                    type="TimeoutError",
                    message=f"pytest timed out after {self.timeout} seconds.",
                ),
                stdout=e.stdout,
                stderr=e.stderr,
            )
        except Exception as e:
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="internal",
                    type=type(e).__name__,
                    message=str(e),
                    traceback=traceback.format_exc(),
                ),
            )

        if proc.returncode == 0:
            return ExecutionEnvironmentResult(
                success=True,
                result=proc.stdout,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )

        return ExecutionEnvironmentResult(
            success=False,
            error=ExecutionEnvironmentErrorDetails(
                stage="test_execution",
                type="TestFailure",
                message=f"pytest exited with code {proc.returncode}",
                traceback=proc.stdout,
            ),
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    # ── Package management ─────────────────────────────────────────────────────

    def install_packages(self, packages: List[str]) -> None:
        """
        Install packages into the running container via docker exec.

        Also commits the container as a new image layer so packages persist
        if the container is restarted.
        """
        new_packages = [p for p in packages if p not in self._installed_packages]
        if not new_packages:
            return

        self._ensure_container()

        # Install directly in the running container
        proc = subprocess.run(
            ["docker", "exec", self._container_id,
             "pip", "install", "--no-cache-dir", *new_packages],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            return

        self._installed_packages.extend(new_packages)

        # Commit the container so the image is updated for future restarts
        new_tag = f"{self._image.split(':')[0]}:latest"
        subprocess.run(
            ["docker", "commit", self._container_id, new_tag],
            capture_output=True, text=True,
        )
        self._image = new_tag

        # Update requirements.txt in the workspace
        req_path = self._workspace_root / "requirements.txt"
        existing = req_path.read_text() if req_path.exists() else ""
        existing_pkgs = {
            line.strip().split("==")[0].split(">=")[0].lower()
            for line in existing.splitlines()
            if line.strip() and not line.startswith("#")
        }

        with open(req_path, "a") as f:
            for pkg in new_packages:
                if pkg.lower() not in existing_pkgs:
                    f.write(f"{pkg}\n")

    def rebuild_image(self, dockerfile_path: str = "Dockerfile") -> bool:
        """Rebuild the Docker image from the service's Dockerfile."""
        full_path = self._workspace_root / dockerfile_path
        if not full_path.exists():
            return False

        tag = self._image
        try:
            subprocess.run(
                [
                    "docker", "build",
                    "-t", tag,
                    "-f", str(full_path),
                    str(self._workspace_root),
                ],
                capture_output=True, text=True, check=True, timeout=300,
            )
            # Restart the container with the new image
            self.stop()
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    # ── Permissions ────────────────────────────────────────────────────────────

    def _fix_permissions(self):
        """Fix file ownership after Docker runs (containers run as root)."""
        if self._container_id is None:
            return
        try:
            subprocess.run(
                ["docker", "exec", self._container_id,
                 "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/workspace/.bizniz"],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass

    # ── Describe ───────────────────────────────────────────────────────────────

    def describe(self) -> str:
        container_status = "running" if self._container_id else "not started"
        return (
            f"DockerPytestEnvironment\n"
            f"Image: {self._image}\n"
            f"Workspace root: {self._workspace_root}\n"
            f"Timeout: {self.timeout}s\n"
            f"Container: {container_status}\n"
            f"Installed packages: {', '.join(self._installed_packages) or 'none'}\n"
        )
