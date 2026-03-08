"""
PytestEnvironment

Runs pytest on a test file via subprocess, returning an ExecutionEnvironmentResult.

Usage in the orchestrator::

    env = PytestEnvironment(workspace_root=workspace.root)
    call_spec = ExecutionCallSpec(symbol="pytest", args=["/abs/path/to/test_file.py"])
    result = env.execute(code="", call_spec=call_spec)

The ``code`` argument is intentionally unused — the test file is already on disk.
The workspace root is added to PYTHONPATH so the test file can import the module
under test using a plain ``import module_name`` statement.
"""

import os
import subprocess
import traceback
from pathlib import Path
from typing import Optional, Union

from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import (
    ExecutionCallSpec,
    ExecutionEnvironmentResult,
    ExecutionEnvironmentErrorDetails,
)


class PytestEnvironment(BaseExecutionEnvironment):
    """
    Executes pytest on a test file path supplied via call_spec.args[0].

    Parameters
    ----------
    workspace_root:
        The workspace root directory.  Added to PYTHONPATH so tests can
        import modules from the workspace without packaging them.
    timeout:
        Maximum seconds to wait for the pytest process (default 120).
    extra_pytest_args:
        Additional arguments forwarded verbatim to pytest (e.g. ``["-x"]``).
    """

    name: str = "pytest-environment"

    def __init__(
        self,
        workspace_root: Union[str, Path],
        timeout: int = 120,
        extra_pytest_args: Optional[list] = None,
    ):
        super().__init__(timeout=timeout)
        self._workspace_root = Path(workspace_root).resolve()
        self._extra_pytest_args = extra_pytest_args or []

    # ── BaseExecutionEnvironment interface ──────────────────────────────────────

    def execute(
        self,
        code: str,  # intentionally unused — tests are already on disk
        call_spec: ExecutionCallSpec,
    ) -> ExecutionEnvironmentResult:
        """
        Run pytest on the file path in call_spec.args[0].

        Returns ExecutionEnvironmentResult with:
        - success=True  if pytest exits with code 0 (all tests passed)
        - success=False if any tests fail, error out, or pytest itself errors
        - stdout / stderr contain the full pytest output
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

        test_path = Path(call_spec.args[0]).resolve()

        if not test_path.exists():
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    type="FileNotFoundError",
                    message=f"Test file not found: {test_path}",
                ),
            )

        cmd = [
            "python3", "-m", "pytest",
            str(test_path),
            "-v",
            "--tb=short",
            "--no-header",
        ] + self._extra_pytest_args

        env = {
            **os.environ,
            "PYTHONPATH": str(self._workspace_root),
        }

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="timeout",
                    type="TimeoutError",
                    message=f"pytest timed out after {self.timeout} seconds.",
                    traceback=None,
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
                traceback=proc.stdout,  # pytest failure detail is in stdout
            ),
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def describe(self) -> str:
        return (
            f"PytestEnvironment\n"
            f"Workspace root (on PYTHONPATH): {self._workspace_root}\n"
            f"Timeout: {self.timeout}s\n"
        )
