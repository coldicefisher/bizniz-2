"""
CodingOrchestrator

Iteratively generates code (via Autocoder) and tests (via Autotester), runs the
tests, and repairs the code on failure until the tests pass or safeguards trigger.

Iteration flow
--------------
1.  Autocoder.generate(prompt, code_filename)   → generate initial code
2.  Autotester.process_from_prompt(prompt, test_filename)  → contract tests
3.  PytestEnvironment.execute(test_file) → run tests
4.  If tests pass → done
5.  If tests fail → Autocoder.repair(code, failure_output, code_filename)
6.  Re-run tests
7.  Repeat 5-6 until success or safeguards fire

Safeguards
----------
- Stale loop: same code hash on two consecutive iterations → OrchestratorStalledError
- Max iterations cap → OrchestratorMaxIterationsError
"""

import hashlib
from typing import Optional, Callable

from bizniz.autocoder.autocoder import Autocoder
from bizniz.autotester.autotester import Autotester
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import ExecutionCallSpec
from bizniz.workspace.base_workspace import BaseWorkspace

from bizniz.orchestrator.types import (
    OrchestratorResult,
    OrchestratorStalledError,
    OrchestratorMaxIterationsError,
)


class CodingOrchestrator:
    """
    Orchestrates Autocoder + Autotester in an iterative repair loop.

    Parameters
    ----------
    autocoder:
        A configured Autocoder instance.
    autotester:
        A configured Autotester instance.
    test_environment:
        An execution environment whose execute() runs pytest on a test file.
        Conventionally a PytestEnvironment; call_spec.args[0] is the absolute
        test file path.
    workspace:
        The shared workspace both agents write to.
    max_iterations:
        Hard cap on the number of code-generate/repair iterations.
    on_status_message:
        Optional callback for human-readable status updates.
    """

    def __init__(
        self,
        autocoder: Autocoder,
        autotester: Autotester,
        test_environment: BaseExecutionEnvironment,
        workspace: BaseWorkspace,
        max_iterations: int = 10,
        on_status_message: Optional[Callable[[str], None]] = None,
    ):
        self._autocoder = autocoder
        self._autotester = autotester
        self._test_environment = test_environment
        self._workspace = workspace
        self._max_iterations = max_iterations
        self._on_status_message = on_status_message

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(
        self,
        prompt: str,
        code_filename: str,
        test_filename: str,
    ) -> OrchestratorResult:
        """
        Run the full iterative coding + testing loop.

        Parameters
        ----------
        prompt:
            The problem statement / feature description.
        code_filename:
            Workspace-relative filename for the generated code module.
        test_filename:
            Workspace-relative filename for the generated test file.

        Returns
        -------
        OrchestratorResult
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        previous_code_hash: Optional[str] = None
        current_code: Optional[str] = None

        # ── Iteration 1: fresh generate ────────────────────────────────────────
        log("Orchestrator: generating initial code...")
        code_result = self._autocoder.generate(
            prompt=prompt,
            filename=code_filename,
        )
        current_code = code_result.code or self._workspace.read_file(code_filename)

        log("Orchestrator: generating contract tests from prompt...")
        test_result = self._autotester.process_from_prompt(
            prompt=prompt,
            output_path=test_filename,
        )
        current_tests = test_result.tests

        # ── Test run + repair loop ─────────────────────────────────────────────
        for iteration in range(1, self._max_iterations + 1):
            log(f"Orchestrator: running tests (iteration {iteration}/{self._max_iterations})...")

            test_abs_path = str(self._workspace.path(test_filename))
            call_spec = ExecutionCallSpec(symbol="pytest", args=[test_abs_path])
            eval_result = self._test_environment.execute(code="", call_spec=call_spec)

            if eval_result.success:
                log(f"Orchestrator: all tests passed after {iteration} iteration(s).")
                return OrchestratorResult(
                    success=True,
                    code=current_code,
                    tests=current_tests,
                    iterations=iteration,
                )

            # Tests failed — extract failure output for repair
            failure_output = _build_failure_message(eval_result)
            log(f"Orchestrator: tests failed — repairing code...\n{failure_output[:400]}")

            # Stale detection before repair
            current_hash = _hash(current_code)
            if current_hash == previous_code_hash:
                raise OrchestratorStalledError(
                    f"Stale loop detected after {iteration} iterations: "
                    "the same code is being produced repeatedly."
                )
            previous_code_hash = current_hash

            # Repair
            repaired = self._autocoder.repair(
                previous_code=current_code,
                error_message=failure_output,
                filename=code_filename,
            )
            current_code = repaired.code or self._workspace.read_file(code_filename)

        raise OrchestratorMaxIterationsError(
            f"Failed to produce passing tests after {self._max_iterations} iterations."
        )

    # ── Public repair helper (also used by AutoEngineer) ──────────────────────

    def strengthen_tests(
        self,
        code_filename: str,
        test_filename: str,
        output_filename: Optional[str] = None,
    ) -> None:
        """
        Run Mode-3 test review to strengthen an existing test suite.
        Saves the improved tests to output_filename (defaults to test_filename).
        """
        output = output_filename or test_filename
        self._autotester.review_tests(
            code_path=code_filename,
            test_path=test_filename,
            output_path=output,
        )


# ── Utilities ──────────────────────────────────────────────────────────────────

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _build_failure_message(eval_result) -> str:
    parts = []
    if eval_result.error:
        parts.append(f"Error: {eval_result.error.type}: {eval_result.error.message}")
        if eval_result.error.traceback:
            parts.append(eval_result.error.traceback)
    if eval_result.stdout:
        parts.append(f"stdout:\n{eval_result.stdout}")
    if eval_result.stderr:
        parts.append(f"stderr:\n{eval_result.stderr}")
    return "\n".join(parts) or "Tests failed with no additional output."
