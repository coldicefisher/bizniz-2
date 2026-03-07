import json
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path
from typing import Optional

from verix.environment.base_environment import BaseExecutionEnvironment
from verix.environment.types import (
    ExecutionEnvironmentResult,
    ExecutionEnvironmentErrorDetails,
    ExecutionCallSpec,
)

from verix.workspace.base_workspace import BaseWorkspace


class DockerExecutionEnvironment(BaseExecutionEnvironment):

    name: str = "docker-python-environment"

    EXEC_ROOT = Path.cwd() / ".verix" / "exec"

    def __init__(
        self,
        image: str = "verix-python-runner",
        network_disabled: bool = True,
        memory_limit: str = "512m",
        cpus: float = 1.0,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.image = image
        self.network_disabled = network_disabled
        self.memory_limit = memory_limit
        self.cpus = cpus

        # Ensure execution directory exists
        self.EXEC_ROOT.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------

    def execute(
        self,
        code: str,
        call_spec: ExecutionCallSpec,
        workspace: Optional[BaseWorkspace] = None,
    ) -> ExecutionEnvironmentResult:

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

                # --------------------------------------------------
                # Write generated code
                # --------------------------------------------------

                code_file = tmp_path / "generated_code.py"
                code_file.write_text(code)

                # --------------------------------------------------
                # Create runner script
                # --------------------------------------------------

                runner_script = tmp_path / "runner.py"
                runner_script.write_text(
                    self._build_runner_script(call_spec)
                )

                # --------------------------------------------------
                # Build docker command
                # --------------------------------------------------

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

                # --------------------------------------------------
                # Execute container
                # --------------------------------------------------

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
                        error=ExecutionEnvironmentErrorDetails(
                            stage="execution",
                            type="RuntimeError",
                            message="Docker execution failed",
                            stdout=proc.stdout,
                            stderr=proc.stderr,
                        ),
                    )

                # --------------------------------------------------
                # Parse output
                # --------------------------------------------------

                try:
                    payload = json.loads(proc.stdout.strip())
                except Exception:
                    return ExecutionEnvironmentResult(
                        success=False,
                        result=None,
                        error=ExecutionEnvironmentErrorDetails(
                            stage="execution",
                            type="InvalidOutput",
                            message="Runner returned invalid JSON",
                            stdout=proc.stdout,
                            stderr=proc.stderr,
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
                        error=ExecutionEnvironmentErrorDetails(
                            stage="execution",
                            type=error.get("type", "RuntimeError"),
                            message=error.get("message", ""),
                            traceback=error.get("traceback"),
                            stdout=proc.stdout,
                            stderr=proc.stderr,
                        ),
                    )

            finally:
                # Cleanup execution folder
                try:
                    shutil.rmtree(tmp_path, ignore_errors=True)
                except Exception:
                    pass

        except subprocess.TimeoutExpired as e:

            return ExecutionEnvironmentResult(
                success=False,
                result=None,
                error=ExecutionEnvironmentErrorDetails(
                    stage="timeout",
                    type="TimeoutError",
                    message=f"Execution exceeded {self.timeout} seconds",
                    stdout=e.stdout,
                    stderr=e.stderr,
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

    # ------------------------------------------------------------------

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