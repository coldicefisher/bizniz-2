"""
DockerJestEnvironment

Runs Jest inside a Docker container with the workspace mounted, so that
Node.js/TypeScript dependencies are available at import time.

Usage in the orchestrator::

    env = DockerJestEnvironment(
        workspace_root=workspace.root,
        image="bizniz-service-frontend:latest",
    )
    call_spec = ExecutionCallSpec(symbol="jest", args=["tests/App.test.tsx"])
    result = env.execute(code="", call_spec=call_spec)

The ``code`` argument is intentionally unused — the test file is already on disk.
The workspace root is bind-mounted at ``/workspace`` inside the container and
tests run with ``npx jest``.
"""

import os
import subprocess
import time
import traceback
from pathlib import Path
from typing import Optional, List, Union

from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import (
    ExecutionCallSpec,
    ExecutionEnvironmentResult,
    ExecutionEnvironmentErrorDetails,
)


class DockerJestEnvironment(BaseExecutionEnvironment):
    """
    Runs Jest inside a Docker container with the workspace mounted.

    Each service workspace has its own Docker image with the correct
    dependencies installed. Tests run inside that container so imports
    of npm packages work correctly.
    """

    name: str = "docker-jest-environment"

    def __init__(
        self,
        workspace_root: Union[Path, str],
        image: str,
        timeout: int = 120,
        extra_jest_args: Optional[List[str]] = None,
        network_enabled: bool = False,
    ):
        super().__init__(timeout=timeout)
        self._workspace_root = Path(workspace_root).resolve()
        self._image = image
        self._extra_jest_args = extra_jest_args or []
        self._network_enabled = network_enabled
        self._installed_packages: List[str] = []

    @property
    def image(self) -> str:
        return self._image

    # ── BaseExecutionEnvironment interface ──────────────────────────────────────

    def execute(
        self,
        code: str,
        call_spec: ExecutionCallSpec,
    ) -> ExecutionEnvironmentResult:
        """
        Run Jest inside the Docker container.

        1. Mount workspace_root at /workspace inside the container
        2. Run: npx jest <test_paths> --verbose --no-cache
        3. Parse exit code and output

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

        # Convert absolute host paths to container paths
        test_paths_container = []
        for arg in call_spec.args:
            if arg.startswith("-"):
                break
            p = Path(arg).resolve()
            try:
                relative = p.relative_to(self._workspace_root)
            except ValueError:
                relative = Path(arg)  # already relative
            test_paths_container.append(f"/workspace/{relative}")

        cmd = [
            "docker", "run", "--rm",
            "-v", f"{self._workspace_root}:/workspace",
            "-w", "/workspace",
        ]

        if not self._network_enabled:
            cmd += ["--network", "none"]

        cmd += [
            self._image,
            "npx", "jest",
            *test_paths_container,
            "--verbose", "--no-cache",
            "--passWithNoTests",
            *self._extra_jest_args,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            self._fix_permissions()
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="timeout",
                    type="TimeoutError",
                    message=f"jest timed out after {self.timeout} seconds.",
                ),
                stdout=e.stdout,
                stderr=e.stderr,
            )
        except Exception as e:
            self._fix_permissions()
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="internal",
                    type=type(e).__name__,
                    message=str(e),
                    traceback=traceback.format_exc(),
                ),
            )

        self._fix_permissions()

        # Jest outputs test results to stderr, combine both
        combined_output = (proc.stdout or "") + (proc.stderr or "")

        if proc.returncode == 0:
            return ExecutionEnvironmentResult(
                success=True,
                result=combined_output,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )

        return ExecutionEnvironmentResult(
            success=False,
            error=ExecutionEnvironmentErrorDetails(
                stage="test_execution",
                type="TestFailure",
                message=f"jest exited with code {proc.returncode}",
                traceback=combined_output,
            ),
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    # ── Package management ─────────────────────────────────────────────────────

    def install_packages(self, packages: List[str]) -> None:
        """
        Install npm packages into the Docker image and update package.json.

        Strategy:
        1. Run ``npm install <packages>`` inside a container based on current image
        2. Commit that container as a new image layer
        3. Update self._image to point to the new image
        """
        new_packages = [p for p in packages if p not in self._installed_packages]
        if not new_packages:
            return

        container_name = f"bizniz-npm-{hash(tuple(new_packages)) & 0xFFFFFFFF:08x}"
        install_cmd = [
            "docker", "run",
            "--name", container_name,
            "-v", f"{self._workspace_root}:/workspace",
            "-w", "/workspace",
            self._image,
            "npm", "install", "--save-dev", *new_packages,
        ]

        try:
            proc = subprocess.run(
                install_cmd, capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                subprocess.run(
                    ["docker", "rm", container_name], capture_output=True,
                )
                return

            # Commit the container as a new image
            new_tag = f"{self._image.split(':')[0]}:latest"
            subprocess.run(
                ["docker", "commit", container_name, new_tag],
                capture_output=True, text=True, check=True,
            )
            self._image = new_tag
            self._installed_packages.extend(new_packages)

        finally:
            subprocess.run(
                ["docker", "rm", container_name], capture_output=True,
            )

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
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    # ── Permissions ────────────────────────────────────────────────────────────

    def _fix_permissions(self):
        """Fix file ownership after Docker runs (containers run as root)."""
        try:
            subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{self._workspace_root}:/workspace",
                    self._image,
                    "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/workspace/.bizniz",
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass

    # ── Describe ───────────────────────────────────────────────────────────────

    def describe(self) -> str:
        return (
            f"DockerJestEnvironment\n"
            f"Image: {self._image}\n"
            f"Workspace root: {self._workspace_root}\n"
            f"Timeout: {self.timeout}s\n"
            f"Installed packages: {', '.join(self._installed_packages) or 'none'}\n"
        )
