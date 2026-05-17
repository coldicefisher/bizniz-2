import hashlib
import json
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path
from typing import Optional, List

from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import (
    ExecutionEnvironmentResult,
    ExecutionEnvironmentErrorDetails,
    ExecutionCallSpec,
)

from bizniz.workspace.base_workspace import BaseWorkspace


# Path to the bundled Dockerfile and requirements for the base image
_DOCKER_DIR = Path(__file__).parent.parent / "docker"
_DOCKERFILE_PATH = _DOCKER_DIR / "Dockerfile.runner"
_BASE_IMAGE = "bizniz-python-runner"


class DockerExecutionEnvironment(BaseExecutionEnvironment):

    name: str = "docker-python-environment"

    # Resolved lazily from ``bizniz.lib.ephemeral.get_exec_root()`` —
    # see that module for the rationale. Was ``Path.cwd() / ".bizniz"
    # / "exec"`` before 2026-05-17; the cwd-rooted default accumulated
    # 774 root-owned ``run_*`` dirs in the bizniz repo because docker
    # created __pycache__ subdirs as root and the host user couldn't
    # delete them. The new location is under
    # ``$XDG_RUNTIME_DIR/bizniz/exec/`` so the OS auto-cleans at
    # logout and ``python -m bizniz.cleanup`` can prune mid-session.
    @classmethod
    def _resolve_exec_root(cls) -> Path:
        from bizniz.lib.ephemeral import get_exec_root
        return get_exec_root()

    def __init__(
        self,
        network_disabled: bool = True,
        memory_limit: str = "512m",
        cpus: float = 1.0,
        additional_packages: Optional[List[str]] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.network_disabled = network_disabled
        self.memory_limit = memory_limit
        self.cpus = cpus
        self._additional_packages: List[str] = list(additional_packages or [])

        # Per-instance EXEC_ROOT — supports test overrides via env var
        # without mutating the class attribute.
        self.EXEC_ROOT = self._resolve_exec_root()

        # Ensure base image exists (build if needed)
        self._ensure_base_image()

        # Set the active image (base or custom with additional packages)
        if self._additional_packages:
            self.image = self._build_custom_image(self._additional_packages)
        else:
            self.image = _BASE_IMAGE

    # ── Image management ──────────────────────────────────────────────────────

    def _ensure_base_image(self):
        """Build the base bizniz-python-runner image if it doesn't exist."""
        if self._image_exists(_BASE_IMAGE):
            return

        if not _DOCKERFILE_PATH.exists():
            raise FileNotFoundError(
                f"Cannot auto-build base image: {_DOCKERFILE_PATH} not found. "
                f"Run 'docker build -t {_BASE_IMAGE} -f {_DOCKERFILE_PATH} {_DOCKER_DIR}' manually."
            )

        subprocess.run(
            [
                "docker", "build",
                "-t", _BASE_IMAGE,
                "-f", str(_DOCKERFILE_PATH),
                str(_DOCKER_DIR),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def install_packages(self, packages: List[str]) -> str:
        """
        Install additional pip packages by building a new Docker image layer.
        Returns the new image tag.
        """
        # Deduplicate with existing packages
        new_packages = [p for p in packages if p not in self._additional_packages]
        if not new_packages:
            return self.image

        self._additional_packages.extend(new_packages)
        self.image = self._build_custom_image(self._additional_packages)
        return self.image

    def _build_custom_image(self, packages: List[str]) -> str:
        """Build a custom image with additional packages on top of the base."""
        # Deterministic tag based on sorted package list
        pkg_hash = hashlib.sha256(
            "\n".join(sorted(packages)).encode()
        ).hexdigest()[:12]
        tag = f"bizniz-custom-{pkg_hash}"

        # Skip if already built
        if self._image_exists(tag):
            return tag

        # Build from base with pip install
        dockerfile_content = (
            f"FROM {_BASE_IMAGE}\n"
            f"RUN pip install --no-cache-dir {' '.join(packages)}\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            dockerfile_path = Path(tmpdir) / "Dockerfile"
            dockerfile_path.write_text(dockerfile_content)

            subprocess.run(
                ["docker", "build", "-t", tag, "-f", str(dockerfile_path), tmpdir],
                check=True,
                capture_output=True,
                text=True,
            )

        return tag

    @staticmethod
    def _image_exists(tag: str) -> bool:
        """Check if a Docker image with the given tag exists locally."""
        result = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    @property
    def installed_packages(self) -> List[str]:
        """Return the list of additional packages installed in the active image."""
        return list(self._additional_packages)

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(
        self,
        code: str,
        call_spec: ExecutionCallSpec,
        workspace: Optional[BaseWorkspace] = None,
    ) -> ExecutionEnvironmentResult:

        # Convert call_spec args to class if dict (for backward compatibility)
        if isinstance(call_spec, dict):
            call_spec = ExecutionCallSpec(**call_spec)

        try:

            tmpdir = tempfile.mkdtemp(prefix="run_", dir=self.EXEC_ROOT)
            tmp_path = Path(tmpdir)

            # Copy workspace files if provided
            workspace_mount = None
            if workspace:
                staged_workspace = tmp_path / "workspace"
                shutil.copytree(workspace.root, staged_workspace)
                workspace_mount = staged_workspace

            try:

                # Write generated code
                code_file = tmp_path / "generated_code.py"
                code_file.write_text(code)

                # Create runner script
                runner_script = tmp_path / "runner.py"
                runner_script.write_text(
                    self._build_runner_script(call_spec)
                )

                # Build docker command
                docker_cmd = [
                    "docker",
                    "run",
                    "--rm",
                    "--memory",
                    self.memory_limit,
                    "--cpus",
                    str(self.cpus),
                    "-v",
                    f"{tmp_path}:/runner",
                    "-w",
                    "/runner",
                ]

                if workspace:
                    docker_cmd += [
                        "-v",
                        f"{workspace_mount}:/workspace",
                    ]

                if self.network_disabled:
                    docker_cmd += ["--network", "none"]

                docker_cmd += [
                    self.image,
                    "python3",
                    "runner.py",
                ]

                # Execute container
                proc = subprocess.run(
                    docker_cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )

                if proc.returncode != 0:
                    return ExecutionEnvironmentResult(
                        success=False,
                        result=None,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                        error=ExecutionEnvironmentErrorDetails(
                            stage="execution",
                            type="RuntimeError",
                            message=proc.stderr.strip() if proc.stderr else "Docker execution failed",
                        ),
                    )

                # Parse output
                try:
                    payload = json.loads(proc.stdout.strip())
                except Exception:
                    return ExecutionEnvironmentResult(
                        success=False,
                        result=None,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                        error=ExecutionEnvironmentErrorDetails(
                            stage="execution",
                            type="InvalidOutput",
                            message="Runner returned invalid JSON",
                        ),
                    )

                if payload.get("success"):
                    return ExecutionEnvironmentResult(
                        success=True,
                        result=payload.get("result"),
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                    )
                else:
                    error = payload.get("error", {})
                    return ExecutionEnvironmentResult(
                        success=False,
                        result=None,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                        error=ExecutionEnvironmentErrorDetails(
                            stage="execution",
                            type=error.get("type", "RuntimeError"),
                            message=error.get("message", ""),
                            traceback=error.get("traceback"),
                        ),
                    )

            finally:
                try:
                    shutil.rmtree(tmp_path, ignore_errors=True)
                except Exception:
                    pass

        except subprocess.TimeoutExpired as e:
            return ExecutionEnvironmentResult(
                success=False,
                result=None,
                stdout=e.stdout if isinstance(e.stdout, str) else None,
                stderr=e.stderr if isinstance(e.stderr, str) else None,
                error=ExecutionEnvironmentErrorDetails(
                    stage="timeout",
                    type="TimeoutError",
                    message=f"Execution exceeded {self.timeout} seconds",
                ),
            )

        except Exception as e:
            return ExecutionEnvironmentResult(
                success=False,
                result=None,
                error=ExecutionEnvironmentErrorDetails(
                    stage="internal",
                    type=type(e).__name__,
                    message=str(e),
                    traceback=traceback.format_exc(),
                ),
            )

    # ── Runner script ─────────────────────────────────────────────────────────

    def _build_runner_script(self, call_spec: ExecutionCallSpec) -> str:

        args = json.dumps(call_spec.args or [])
        kwargs = json.dumps(call_spec.kwargs or {})

        return f"""
import json
import traceback
import generated_code

try:

    args = {args}
    kwargs = {kwargs}

    fn = getattr(generated_code, "{call_spec.symbol}")

    result = fn(*args, **kwargs)

    print(json.dumps({{
        "success": True,
        "result": result
    }}))

except Exception as e:

    print(json.dumps({{
        "success": False,
        "error": {{
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc()
        }}
    }}))
"""
