"""
DockerJestEnvironment

Runs Jest inside a persistent Docker container with the workspace mounted,
so that Node.js/TypeScript dependencies are available at import time.

The container is started once (lazily on first execute()) and reused for all
subsequent test runs via ``docker exec``. This eliminates container startup
overhead which compounds across many iterations.

Usage in the orchestrator::

    env = DockerJestEnvironment(
        workspace_root=workspace.root,
        image="bizniz-service-frontend:latest",
    )
    call_spec = ExecutionCallSpec(symbol="jest", args=["tests/App.test.tsx"])
    result = env.execute(code="", call_spec=call_spec)

    # When done, clean up:
    env.stop()

The ``code`` argument is intentionally unused — the test file is already on disk.
The workspace root is bind-mounted at ``/workspace`` inside the container and
tests run with ``npx jest``.
"""

import subprocess
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


class DockerJestEnvironment(BaseExecutionEnvironment):
    """
    Runs Jest inside a persistent Docker container with the workspace mounted.

    The container is started lazily on the first execute() call and kept alive
    for all subsequent runs. Use stop() to clean up, or use as a context manager.
    """

    name: str = "docker-jest-environment"

    def __init__(
        self,
        workspace_root: Union[Path, str],
        image: str,
        timeout: int = 120,
        extra_jest_args: Optional[List[str]] = None,
        network_enabled: bool = True,
    ):
        super().__init__(timeout=timeout)
        self._workspace_root = Path(workspace_root).resolve()
        self._image = image
        self._extra_jest_args = extra_jest_args or []
        self._network_enabled = network_enabled
        self._installed_packages: List[str] = []
        self._container_id: Optional[str] = None
        self._container_name = f"bizniz-jest-{uuid.uuid4().hex[:12]}"

    @property
    def image(self) -> str:
        return self._image

    # ── Container lifecycle ────────────────────────────────────────────────────

    def _ensure_container(self):
        """Start the persistent container if not already running."""
        if self._container_id is not None:
            check = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._container_id],
                capture_output=True, text=True,
            )
            if check.returncode == 0 and "true" in check.stdout.strip().lower():
                return
            else:
                self._container_id = None

        # Clean up any stopped bizniz-jest containers from prior crashed runs
        self._cleanup_stale_containers()

        # Generate fresh container name for each start
        self._container_name = f"bizniz-jest-{uuid.uuid4().hex[:12]}"

        cmd = [
            "docker", "run", "-d", "--rm",
            "--name", self._container_name,
            "-w", "/workspace",
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

        # Create /workspace dir and sync files into container
        subprocess.run(
            ["docker", "exec", self._container_id, "mkdir", "-p", "/workspace"],
            capture_output=True, timeout=10,
        )
        self._sync_workspace()

    def _sync_workspace(self):
        """Copy workspace files into the container via docker cp."""
        if self._container_id is None:
            return
        src = f"{self._workspace_root}/."
        proc = subprocess.run(
            ["docker", "cp", src, f"{self._container_id}:/workspace/"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to sync workspace to container: {proc.stderr.strip()}"
            )

    def stop(self):
        """Stop and remove the persistent container."""
        if self._container_id is not None:
            self._sync_workspace_from_container()
            subprocess.run(
                ["docker", "rm", "-f", self._container_id],
                capture_output=True, timeout=10,
            )
            self._container_id = None

    @staticmethod
    def _cleanup_stale_containers():
        """Remove stopped bizniz-jest containers left by crashed runs."""
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--filter", "name=bizniz-jest-",
                 "--filter", "status=exited", "--filter", "status=created",
                 "-q"],
                capture_output=True, text=True, timeout=10,
            )
            container_ids = result.stdout.strip().split("\n")
            container_ids = [c for c in container_ids if c]
            if container_ids:
                subprocess.run(
                    ["docker", "rm", "-f"] + container_ids,
                    capture_output=True, timeout=15,
                )
        except Exception:
            pass  # Best-effort cleanup

    def _sync_workspace_from_container(self):
        """Copy workspace files back from container to host (e.g. generated files).

        Excludes .bizniz/ directory since the workspace DB is managed by the
        host process, not the container.
        """
        if self._container_id is None:
            return
        try:
            # Use tar to exclude .bizniz/ dir when copying back
            proc = subprocess.run(
                ["docker", "exec", self._container_id,
                 "tar", "-cf", "-", "--exclude=.bizniz", "-C", "/workspace", "."],
                capture_output=True, timeout=60,
            )
            if proc.returncode == 0 and proc.stdout:
                import tarfile
                import io
                tar = tarfile.open(fileobj=io.BytesIO(proc.stdout))
                tar.extractall(path=str(self._workspace_root))
                tar.close()
        except Exception:
            pass

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
        Run Jest inside the persistent Docker container via docker exec.

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

        # Sync latest workspace files into container before running tests
        try:
            self._sync_workspace()
        except Exception as e:
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="workspace_sync",
                    type=type(e).__name__,
                    message=f"Failed to sync workspace: {e}",
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
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="internal",
                    type=type(e).__name__,
                    message=str(e),
                    traceback=traceback.format_exc(),
                ),
            )

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
        Install npm packages into the running container via docker exec.

        Temporarily connects the container to the bridge network if it was
        started with --network none, then disconnects after installation.

        Also commits the container as a new image layer so packages persist
        if the container is restarted.
        """
        new_packages = [p for p in packages if p not in self._installed_packages]
        if not new_packages:
            return

        self._ensure_container()

        # Temporarily enable network if container was started without it
        needs_network_restore = False
        if not self._network_enabled:
            subprocess.run(
                ["docker", "network", "connect", "bridge", self._container_id],
                capture_output=True, timeout=10,
            )
            needs_network_restore = True

        try:
            proc = subprocess.run(
                ["docker", "exec", "-w", "/workspace", self._container_id,
                 "npm", "install", "--save-dev", *new_packages],
                capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                return
        finally:
            # Restore network isolation
            if needs_network_restore:
                subprocess.run(
                    ["docker", "network", "disconnect", "bridge", self._container_id],
                    capture_output=True, timeout=10,
                )

        self._installed_packages.extend(new_packages)

        # Commit the container so the image is updated for future restarts
        new_tag = f"{self._image.split(':')[0]}:latest"
        subprocess.run(
            ["docker", "commit", self._container_id, new_tag],
            capture_output=True, text=True,
        )
        self._image = new_tag

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

    # ── Describe ───────────────────────────────────────────────────────────────

    def describe(self) -> str:
        container_status = "running" if self._container_id else "not started"
        return (
            f"DockerJestEnvironment\n"
            f"Image: {self._image}\n"
            f"Workspace root: {self._workspace_root}\n"
            f"Timeout: {self.timeout}s\n"
            f"Container: {container_status}\n"
            f"Installed packages: {', '.join(self._installed_packages) or 'none'}\n"
        )
