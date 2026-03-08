"""
CodingOrchestrator

Iteratively generates code (via Autocoder) and tests (via Autotester), runs the
tests, and repairs the code on failure until the tests pass or safeguards trigger.

Features
--------
- Autodebugger-driven diagnosis: determines whether to fix code or tests
- Model escalation: starts with a cheap model, escalates to stronger models on stalls
- Missing package detection: auto-installs packages and persists to workspace DB
- Heuristic fallback when no autodebugger is provided

Iteration flow
--------------
1.  Autocoder.generate_only(prompt, code_filename)  → generate initial code
2.  Autotester.process_from_prompt(prompt, test_filename) → contract tests
3.  PytestEnvironment.execute(test_file) → run tests
4.  If tests pass → done
5.  If tests fail → Autodebugger.diagnose() → determine fix target
6.  If fix_target is "code" → Autocoder.repair(code, diagnosis, code_filename)
7.  If fix_target is "tests" → Autotester.process_from_prompt(enriched_prompt)
8.  Repeat 3-7 until success or safeguards fire

Safeguards
----------
- Stale loop: same code hash on two consecutive iterations → regenerate tests
- Model escalation: on stalls, switch to a stronger model
- Max iterations cap → OrchestratorMaxIterationsError
"""

import hashlib
import re
from pathlib import Path
from typing import Optional, Callable, List

from bizniz.autocoder.autocoder import Autocoder
from bizniz.autodebugger.autodebugger import Autodebugger
from bizniz.autotester.autotester import Autotester
from bizniz.clients.chatgpt.base_chatgpt_client import BaseChatGPTClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import ExecutionCallSpec
from bizniz.orchestrator.model_progression import ModelProgression
from bizniz.workspace.base_workspace import BaseWorkspace

from bizniz.autocoder.types import FileChange
from bizniz.autotester.types import GeneratedTestFile
from bizniz.orchestrator.types import (
    OrchestratorResult,
    OrchestratorStalledError,
    OrchestratorMaxIterationsError,
)


class CodingOrchestrator:
    """
    Orchestrates Autocoder + Autotester + Autodebugger in an iterative repair loop.

    Parameters
    ----------
    autocoder:
        A configured Autocoder instance.
    autotester:
        A configured Autotester instance.
    autodebugger:
        Optional Autodebugger instance for intelligent failure diagnosis.
    test_environment:
        An execution environment whose execute() runs pytest on a test file.
    workspace:
        The shared workspace both agents write to.
    client:
        Optional shared AI client reference. Required for model escalation.
    model_progression:
        Optional ModelProgression for escalating to stronger models on stalls.
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
        autodebugger: Optional[Autodebugger] = None,
        client: Optional[BaseChatGPTClient] = None,
        model_progression: Optional[ModelProgression] = None,
        max_iterations: int = 20,
        on_status_message: Optional[Callable[[str], None]] = None,
    ):
        self._autocoder = autocoder
        self._autotester = autotester
        self._autodebugger = autodebugger
        self._test_environment = test_environment
        self._workspace = workspace
        self._client = client
        self._model_progression = model_progression
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
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Load persisted packages from workspace DB into the Docker environment
        self._sync_environment_packages(log)

        previous_code_hash: Optional[str] = None
        current_code: Optional[str] = None
        stale_count: int = 0

        # ── Iteration 1: fresh generate ────────────────────────────────────────
        log("Orchestrator: generating initial code...")
        code_result = self._autocoder.generate_only(
            prompt=prompt,
            filename=code_filename,
        )
        current_code = _extract_code(code_result.changes, code_filename) or self._workspace.read_file(code_filename)

        log("Orchestrator: generating contract tests from prompt...")
        test_result = self._autotester.process_from_prompt(
            prompt=prompt,
            output_path=test_filename,
            code_filename=code_filename,
        )
        current_tests = _extract_tests(test_result.test_files, test_filename)

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
                    changes=[FileChange(filepath=code_filename, code=current_code, action="create")],
                    test_files=[GeneratedTestFile(filepath=test_filename, tests=current_tests)],
                    iterations=iteration,
                )

            # Tests failed — extract failure output
            failure_output = _build_failure_message(eval_result, test_code=current_tests)
            log(f"Orchestrator: tests failed —\n{failure_output}")

            # ── Missing package detection ─────────────────────────────────────
            missing_pkg = _detect_missing_package(failure_output)
            if missing_pkg:
                # Don't try to pip-install workspace modules
                workspace_files = [str(f) for f in self._workspace.list_relative_files()]
                is_workspace_module = any(
                    f == missing_pkg + ".py"
                    or f.startswith(missing_pkg + "/")
                    or f == missing_pkg + "/__init__.py"
                    for f in workspace_files
                )
                if not is_workspace_module:
                    log(f"Orchestrator: detected missing package '{missing_pkg}', installing...")
                    self._install_package(missing_pkg, log)
                    continue  # Re-run tests without counting as a failed iteration

            # ── Collection error → regenerate tests ───────────────────────────
            is_collection_error = (
                eval_result.error
                and eval_result.error.message
                and "exited with code 2" in eval_result.error.message
            )
            if is_collection_error:
                log("Orchestrator: test collection error — regenerating tests...")
                error_detail = ""
                if eval_result.error and eval_result.error.traceback:
                    error_detail = eval_result.error.traceback
                elif eval_result.stdout:
                    error_detail = eval_result.stdout

                regen_prompt = (
                    f"{prompt}\n\n"
                    f"Here is the current implementation that tests must be written for:\n"
                    f"```python\n{current_code}\n```\n\n"
                    f"IMPORTANT: The previous test file had errors and could not be collected by pytest.\n"
                    f"The error was:\n{error_detail}\n\n"
                    f"Make sure all test functions use only defined fixtures or pytest.mark.parametrize.\n"
                    f"Do NOT use undefined fixture parameters in test function signatures."
                )
                test_result = self._autotester.process_from_prompt(
                    prompt=regen_prompt,
                    output_path=test_filename,
                    code_filename=code_filename,
                )
                current_tests = _extract_tests(test_result.test_files, test_filename)
                stale_count = 0
                previous_code_hash = None
                continue

            # ── Autodebugger-driven diagnosis ─────────────────────────────────
            if self._autodebugger is not None:
                current_code, current_tests, stale_count, previous_code_hash = (
                    self._handle_failure_with_debugger(
                        prompt=prompt,
                        failure_output=failure_output,
                        current_code=current_code,
                        current_tests=current_tests,
                        code_filename=code_filename,
                        test_filename=test_filename,
                        stale_count=stale_count,
                        previous_code_hash=previous_code_hash,
                        iteration=iteration,
                        log=log,
                    )
                )
                continue

            # ── Heuristic fallback (no autodebugger) ──────────────────────────
            current_code, current_tests, stale_count, previous_code_hash = (
                self._handle_failure_heuristic(
                    prompt=prompt,
                    failure_output=failure_output,
                    current_code=current_code,
                    current_tests=current_tests,
                    code_filename=code_filename,
                    test_filename=test_filename,
                    stale_count=stale_count,
                    previous_code_hash=previous_code_hash,
                    iteration=iteration,
                    log=log,
                )
            )

        raise OrchestratorMaxIterationsError(
            f"Failed to produce passing tests after {self._max_iterations} iterations."
        )

    # ── Multi-file API ─────────────────────────────────────────────────────────

    def run_multi(
        self,
        prompt: str,
        target_files: List[dict],
        test_files: List[str],
        architecture_context: str = "",
    ) -> OrchestratorResult:
        """
        Run the full iterative coding + testing loop for a multi-file issue.

        Parameters
        ----------
        prompt:
            The issue description / task.
        target_files:
            List of {"filepath": "...", "action": "create"|"modify"} dicts.
        test_files:
            List of test file paths to generate.
        architecture_context:
            Formatted architecture plan string for context.
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        self._sync_environment_packages(log)

        stale_count = 0
        previous_code_hash: Optional[str] = None

        # ── Load existing code for files being modified ──────────────────────
        existing_code = {}
        for tf in target_files:
            if tf.get("action") == "modify" and self._workspace.exists(path=tf["filepath"]):
                existing_code[tf["filepath"]] = self._workspace.read_file(path=tf["filepath"])

        # ── Snapshot passing tests before we start (regression baseline) ─────
        baseline_passing = self._get_passing_tests(log)

        # ── Generate initial code ────────────────────────────────────────────
        log(f"Orchestrator: generating code for {len(target_files)} file(s)...")
        code_result = self._autocoder.generate_multi(
            issue_description=prompt,
            target_files=target_files,
            architecture_context=architecture_context,
            existing_code=existing_code,
        )
        current_files = {ch.filepath: ch.code for ch in code_result.changes}

        # ── Generate tests ───────────────────────────────────────────────────
        log(f"Orchestrator: generating {len(test_files)} test file(s)...")
        test_result = self._autotester.generate_multi(
            problem_statement=prompt,
            test_files=test_files,
            source_code=current_files,
            architecture_context=architecture_context,
        )
        current_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}

        # ── Test + repair loop ───────────────────────────────────────────────
        for iteration in range(1, self._max_iterations + 1):
            log(f"Orchestrator: running tests (iteration {iteration}/{self._max_iterations})...")

            test_abs_paths = [
                str(self._workspace.path(tf)) for tf in current_test_files.keys()
            ]
            call_spec = ExecutionCallSpec(symbol="pytest", args=test_abs_paths)
            eval_result = self._test_environment.execute(code="", call_spec=call_spec)

            if eval_result.success:
                # Check for regressions against baseline
                regressions = self._detect_regressions(baseline_passing, log)

                # Check for architecture drift
                drift = self._detect_drift(target_files, code_result.changes)

                if regressions:
                    log(f"Orchestrator: {len(regressions)} regression(s) detected: {regressions}")
                    failure_output = (
                        f"Tests passed but REGRESSIONS detected in: {', '.join(regressions)}\n"
                        f"These tests were passing before your changes and now fail.\n"
                        f"Fix the code to make all tests pass without breaking existing functionality."
                    )
                    current_files, current_test_files, stale_count, previous_code_hash = (
                        self._handle_multi_failure(
                            prompt=prompt,
                            failure_output=failure_output,
                            current_files=current_files,
                            current_test_files=current_test_files,
                            target_files=target_files,
                            test_files=list(current_test_files.keys()),
                            architecture_context=architecture_context,
                            stale_count=stale_count,
                            previous_code_hash=previous_code_hash,
                            log=log,
                        )
                    )
                    continue

                log(f"Orchestrator: all tests passed after {iteration} iteration(s).")
                return OrchestratorResult(
                    success=True,
                    changes=[
                        FileChange(filepath=fp, code=code, action="create")
                        for fp, code in current_files.items()
                    ],
                    test_files=[
                        GeneratedTestFile(filepath=fp, tests=tests)
                        for fp, tests in current_test_files.items()
                    ],
                    iterations=iteration,
                    architecture_drift_detected=bool(drift),
                    drift_files=drift,
                )

            # Tests failed
            failure_output = _build_failure_message_multi(eval_result, current_test_files)
            log(f"Orchestrator: tests failed (iteration {iteration})")

            # ── Missing package detection ─────────────────────────────────
            missing_pkg = _detect_missing_package(failure_output)
            if missing_pkg:
                workspace_files = [str(f) for f in self._workspace.list_relative_files()]
                is_workspace_module = any(
                    f == missing_pkg + ".py"
                    or f.startswith(missing_pkg + "/")
                    or f == missing_pkg + "/__init__.py"
                    for f in workspace_files
                )
                if not is_workspace_module:
                    log(f"Orchestrator: installing missing package '{missing_pkg}'...")
                    self._install_package(missing_pkg, log)
                    continue

            # ── Collection error → regenerate tests ───────────────────────
            is_collection_error = (
                eval_result.error
                and eval_result.error.message
                and "exited with code 2" in eval_result.error.message
            )
            if is_collection_error:
                log("Orchestrator: test collection error — regenerating tests...")
                error_detail = ""
                if eval_result.error and eval_result.error.traceback:
                    error_detail = eval_result.error.traceback
                elif eval_result.stdout:
                    error_detail = eval_result.stdout

                enriched_prompt = (
                    f"{prompt}\n\n"
                    f"CURRENT CODE:\n"
                    + "\n".join(f"── {fp} ──\n```python\n{code}\n```" for fp, code in current_files.items())
                    + f"\n\nThe previous tests had collection errors:\n{error_detail}\n"
                    f"Fix the imports and test structure."
                )
                test_result = self._autotester.generate_multi(
                    problem_statement=enriched_prompt,
                    test_files=list(current_test_files.keys()),
                    source_code=current_files,
                    architecture_context=architecture_context,
                )
                current_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}
                stale_count = 0
                previous_code_hash = None
                continue

            # ── Repair ────────────────────────────────────────────────────
            current_files, current_test_files, stale_count, previous_code_hash = (
                self._handle_multi_failure(
                    prompt=prompt,
                    failure_output=failure_output,
                    current_files=current_files,
                    current_test_files=current_test_files,
                    target_files=target_files,
                    test_files=list(current_test_files.keys()),
                    architecture_context=architecture_context,
                    stale_count=stale_count,
                    previous_code_hash=previous_code_hash,
                    log=log,
                )
            )

        raise OrchestratorMaxIterationsError(
            f"Failed to produce passing tests after {self._max_iterations} iterations."
        )

    # ── Multi-file failure handling ───────────────────────────────────────────

    def _handle_multi_failure(
        self,
        prompt: str,
        failure_output: str,
        current_files: dict,
        current_test_files: dict,
        target_files: List[dict],
        test_files: List[str],
        architecture_context: str,
        stale_count: int,
        previous_code_hash: Optional[str],
        log: Callable,
    ) -> tuple:
        """
        Handle a test failure in multi-file mode. Uses autodebugger if available,
        otherwise repairs code directly.

        Returns (current_files, current_test_files, stale_count, previous_code_hash).
        """
        # Compute combined hash for stale detection
        combined = "".join(sorted(f"{k}:{v}" for k, v in current_files.items()))
        current_hash = _hash(combined)

        if current_hash == previous_code_hash:
            stale_count += 1
            if stale_count >= 2:
                self._try_escalate_model(log)
                # Regenerate tests on stall
                log("Orchestrator: multi-file repair stalled — regenerating tests...")
                test_result = self._autotester.generate_multi(
                    problem_statement=prompt + f"\n\nPrevious failures:\n{failure_output}",
                    test_files=test_files,
                    source_code=current_files,
                    architecture_context=architecture_context,
                )
                new_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}
                return current_files, new_test_files, 0, None
        else:
            stale_count = 0

        # Repair code across all files
        log("Orchestrator: repairing code across files...")
        all_files = {**current_files}
        # Include test files in context for the repair
        for fp, tests in current_test_files.items():
            all_files[fp] = tests

        repaired = self._autocoder.repair_multi(
            current_files=all_files,
            error_message=failure_output,
            architecture_context=architecture_context,
        )

        # Apply changes — only update files that were changed
        new_files = dict(current_files)
        new_test_files = dict(current_test_files)
        for ch in repaired.changes:
            if ch.filepath in new_test_files:
                new_test_files[ch.filepath] = ch.code
            else:
                new_files[ch.filepath] = ch.code
                # Save updated code to workspace
                self._workspace.write_file(path=ch.filepath, content=ch.code)

        new_hash = _hash("".join(sorted(f"{k}:{v}" for k, v in new_files.items())))
        return new_files, new_test_files, stale_count, new_hash

    # ── Regression detection ──────────────────────────────────────────────────

    def _get_passing_tests(self, log: Callable) -> set:
        """
        Run all existing tests in the workspace and return the set of
        test file paths that pass. Used as a baseline for regression detection.
        """
        try:
            test_paths = [
                str(f) for f in self._workspace.list_relative_files()
                if str(f).startswith("tests/") and str(f).endswith(".py") and str(f) != "tests/__init__.py"
            ]
            if not test_paths:
                return set()

            abs_paths = [str(self._workspace.path(tp)) for tp in test_paths]
            existing_paths = [p for p in abs_paths if Path(p).exists()]
            if not existing_paths:
                return set()

            passing = set()
            for test_path in existing_paths:
                call_spec = ExecutionCallSpec(symbol="pytest", args=[test_path])
                result = self._test_environment.execute(code="", call_spec=call_spec)
                if result.success:
                    # Store relative path
                    rel = str(Path(test_path).relative_to(self._workspace.path(".")))
                    passing.add(rel)

            if passing:
                log(f"Orchestrator: baseline — {len(passing)} test file(s) currently passing")
            return passing
        except Exception:
            return set()

    def _detect_regressions(self, baseline_passing: set, log: Callable) -> List[str]:
        """
        Re-run baseline-passing tests and return any that now fail.
        """
        if not baseline_passing:
            return []

        regressions = []
        for rel_path in baseline_passing:
            abs_path = str(self._workspace.path(rel_path))
            if not Path(abs_path).exists():
                continue
            call_spec = ExecutionCallSpec(symbol="pytest", args=[abs_path])
            result = self._test_environment.execute(code="", call_spec=call_spec)
            if not result.success:
                regressions.append(rel_path)

        return regressions

    # ── Drift detection ───────────────────────────────────────────────────────

    @staticmethod
    def _detect_drift(
        planned_files: List[dict],
        actual_changes: List[FileChange],
    ) -> List[str]:
        """
        Compare actual file changes against the planned target files.
        Returns list of unplanned filepaths that were changed.
        """
        planned_paths = {tf["filepath"] for tf in planned_files}
        unplanned = [
            ch.filepath for ch in actual_changes
            if ch.filepath not in planned_paths
        ]
        return unplanned

    # ── Model escalation ──────────────────────────────────────────────────────

    def _try_escalate_model(self, log: Callable) -> bool:
        """
        Attempt to escalate to the next model in the progression.
        Returns True if escalation happened, False if already at max or not configured.
        """
        if not self._model_progression or not self._client:
            return False
        if self._model_progression.is_at_max:
            return False

        new_model = self._model_progression.escalate()
        if new_model:
            self._client.set_model(new_model)
            log(f"Orchestrator: escalated to model {new_model}")
            return True
        return False

    # ── Environment / package management ──────────────────────────────────────

    def _sync_environment_packages(self, log: Callable):
        """Load persisted packages from workspace DB and install into Docker env."""
        try:
            rows = self._workspace.db.get_packages()
            if rows:
                packages = [row["package"] for row in rows]
                # Install into Docker environment if it supports it
                docker_env = self._find_docker_environment()
                if docker_env:
                    docker_env.install_packages(packages)
                    log(f"Orchestrator: synced {len(packages)} package(s) from workspace DB")
        except Exception:
            pass  # DB may not exist yet for fresh workspaces

    def _install_package(self, package: str, log: Callable):
        """Install a package into the Docker environment and persist to DB."""
        docker_env = self._find_docker_environment()
        if docker_env:
            docker_env.install_packages([package])
            log(f"Orchestrator: installed package '{package}' into Docker image")

        # Persist to workspace DB
        try:
            self._workspace.db.save_package(package)
        except Exception:
            pass

    def _find_docker_environment(self):
        """Find a DockerExecutionEnvironment among the environments we have."""
        from bizniz.environment.docker_environment import DockerExecutionEnvironment
        # Check if test_environment or autocoder's environment is Docker
        if isinstance(self._test_environment, DockerExecutionEnvironment):
            return self._test_environment
        if hasattr(self._autocoder, '_environment') and isinstance(self._autocoder._environment, DockerExecutionEnvironment):
            return self._autocoder._environment
        return None

    # ── Failure handling strategies ────────────────────────────────────────────

    def _handle_failure_with_debugger(
        self,
        prompt: str,
        failure_output: str,
        current_code: str,
        current_tests: str,
        code_filename: str,
        test_filename: str,
        stale_count: int,
        previous_code_hash: Optional[str],
        iteration: int,
        log: Callable,
    ) -> tuple:
        """
        Use the Autodebugger to diagnose the failure and decide whether to
        repair code or regenerate tests.

        Returns (current_code, current_tests, stale_count, previous_code_hash).
        """
        log("Orchestrator: running autodebugger diagnosis...")

        try:
            diagnosis = self._autodebugger.diagnose(
                error_output=failure_output,
                code=current_code,
                code_filename=code_filename,
                test_code=current_tests,
                test_filename=test_filename,
            )
        except Exception as e:
            log(f"Orchestrator: autodebugger failed ({e}), falling back to code repair...")
            new_code, new_stale, new_hash = self._repair_code(
                failure_output=failure_output,
                current_code=current_code,
                code_filename=code_filename,
                stale_count=stale_count,
                previous_code_hash=previous_code_hash,
            )
            return new_code, current_tests, new_stale, new_hash

        log(f"Orchestrator: diagnosis — fix_target={diagnosis.fix_target}")
        log(f"Orchestrator: {diagnosis.diagnosis}")

        if diagnosis.fix_target == "tests":
            log("Orchestrator: regenerating tests based on diagnosis...")
            related_context = ""
            if diagnosis.relevant_files:
                parts = []
                for fname, summary in diagnosis.relevant_files.items():
                    parts.append(f"- {fname}: {summary}")
                related_context = "\nRELATED FILES:\n" + "\n".join(parts) + "\n"

            regen_prompt = (
                f"REQUIREMENTS (source of truth — tests must verify these):\n"
                f"──────────────────────────────────────────────────────────────\n"
                f"{prompt}\n\n"
                f"CURRENT CODE (use for imports and function signatures only):\n"
                f"──────────────────────────────────────────────────────────────\n"
                f"```python\n{current_code}\n```\n\n"
                f"{related_context}"
                f"DIAGNOSIS:\n"
                f"──────────────────────────────────────────────────────────────\n"
                f"{diagnosis.diagnosis}\n\n"
                f"SUGGESTED APPROACH:\n{diagnosis.suggested_approach}\n\n"
                f"Write NEW tests that:\n"
                f"- Verify the REQUIREMENTS above, not the code's current behavior\n"
                f"- Use the correct imports and function/class signatures from the code\n"
                f"- Do NOT hardcode implementation-specific values\n"
                f"- Do NOT access private attributes or internal data structures\n"
                f"- Test the public interface and expected behavior from the requirements\n"
                f"- Keep tests simple and focused on correctness"
            )
            test_result = self._autotester.process_from_prompt(
                prompt=regen_prompt,
                output_path=test_filename,
                code_filename=code_filename,
            )
            return current_code, _extract_tests(test_result.test_files, test_filename), 0, None

        else:
            # fix_target == "code"
            enriched_error = (
                f"AUTODEBUGGER DIAGNOSIS:\n"
                f"──────────────────────────────────────────────────────────────\n"
                f"{diagnosis.diagnosis}\n\n"
                f"SUGGESTED APPROACH:\n{diagnosis.suggested_approach}\n\n"
            )

            if diagnosis.relevant_files:
                enriched_error += "RELATED FILES IN WORKSPACE:\n"
                for fname, summary in diagnosis.relevant_files.items():
                    enriched_error += f"── {fname}: {summary}\n"
                    try:
                        content = self._workspace.read_file(path=fname)
                        if content:
                            enriched_error += f"```python\n{content}\n```\n\n"
                    except Exception:
                        pass

            enriched_error += (
                f"ORIGINAL ERROR OUTPUT:\n"
                f"──────────────────────────────────────────────────────────────\n"
                f"{failure_output}"
            )

            repaired = self._autocoder.repair(
                previous_code=current_code,
                error_message=enriched_error,
                filename=code_filename,
            )
            new_code = _extract_code(repaired.changes, code_filename) or self._workspace.read_file(code_filename)

            # Update stale detection
            current_hash = _hash(current_code)
            new_hash = _hash(new_code)
            if new_hash == current_hash:
                stale_count += 1
                # Escalate model on stall
                if stale_count >= 2:
                    self._try_escalate_model(log)
                    stale_count = 0
            else:
                stale_count = 0
            previous_code_hash = new_hash

            return new_code, current_tests, stale_count, previous_code_hash

    def _handle_failure_heuristic(
        self,
        prompt: str,
        failure_output: str,
        current_code: str,
        current_tests: str,
        code_filename: str,
        test_filename: str,
        stale_count: int,
        previous_code_hash: Optional[str],
        iteration: int,
        log: Callable,
    ) -> tuple:
        """
        Original heuristic-based failure handling (no autodebugger).

        Returns (current_code, current_tests, stale_count, previous_code_hash).
        """
        current_hash = _hash(current_code)
        if current_hash == previous_code_hash:
            stale_count += 1
            if stale_count >= 2:
                # Try escalating model first
                escalated = self._try_escalate_model(log)

                log("Orchestrator: code repair stalled — regenerating tests from requirements...")
                regen_prompt = (
                    f"REQUIREMENTS (source of truth — tests must verify these):\n"
                    f"──────────────────────────────────────────────────────────────\n"
                    f"{prompt}\n\n"
                    f"CURRENT CODE (use for imports and function signatures only):\n"
                    f"──────────────────────────────────────────────────────────────\n"
                    f"```python\n{current_code}\n```\n\n"
                    f"The previous tests failed and the code could not be repaired to pass them.\n"
                    f"The failures were:\n{failure_output}\n\n"
                    f"Write NEW tests that:\n"
                    f"- Verify the REQUIREMENTS above, not the code's current behavior\n"
                    f"- Use the correct imports and function/class signatures from the code\n"
                    f"- Do NOT hardcode implementation-specific values (hashes, encodings, internal state)\n"
                    f"- Do NOT access private attributes or internal data structures\n"
                    f"- Test the public interface and expected behavior from the requirements\n"
                    f"- Keep tests simple and focused on correctness"
                )
                test_result = self._autotester.process_from_prompt(
                    prompt=regen_prompt,
                    output_path=test_filename,
                    code_filename=code_filename,
                )
                return current_code, _extract_tests(test_result.test_files, test_filename), 0, None
        else:
            stale_count = 0
        previous_code_hash = current_hash

        # Escalate at halfway point if still failing
        if iteration == self._max_iterations // 2:
            self._try_escalate_model(log)

        # Repair code
        repaired = self._autocoder.repair(
            previous_code=current_code,
            error_message=failure_output,
            filename=code_filename,
        )
        new_code = _extract_code(repaired.changes, code_filename) or self._workspace.read_file(code_filename)

        return new_code, current_tests, stale_count, previous_code_hash

    def _repair_code(
        self,
        failure_output: str,
        current_code: str,
        code_filename: str,
        stale_count: int,
        previous_code_hash: Optional[str],
    ) -> tuple:
        """Simple code repair, returns (new_code, stale_count, previous_code_hash)."""
        repaired = self._autocoder.repair(
            previous_code=current_code,
            error_message=failure_output,
            filename=code_filename,
        )
        new_code = _extract_code(repaired.changes, code_filename) or self._workspace.read_file(code_filename)
        current_hash = _hash(current_code)
        new_hash = _hash(new_code)
        if new_hash == current_hash:
            stale_count += 1
        else:
            stale_count = 0
        return new_code, stale_count, new_hash

    # ── Public repair helper (also used by AutoEngineer) ──────────────────────

    def strengthen_tests(
        self,
        code_filename: str,
        test_filename: str,
        output_filename: Optional[str] = None,
    ) -> None:
        """
        Run Mode-3 test review to strengthen an existing test suite.
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


def _detect_missing_package(error_output: str) -> Optional[str]:
    """
    Parse error output for ModuleNotFoundError / ImportError indicating
    a missing pip package. Returns the package name or None.
    """
    # ModuleNotFoundError: No module named 'requests'
    match = re.search(r"ModuleNotFoundError: No module named '(\w+)'", error_output)
    if match:
        return match.group(1)
    # ImportError: No module named 'numpy'
    match = re.search(r"ImportError: No module named '(\w+)'", error_output)
    if match:
        return match.group(1)
    return None


def _extract_code(changes: list, filename: str) -> Optional[str]:
    """Extract code for a specific file from a list of FileChange objects."""
    for change in changes:
        if change.filepath == filename:
            return change.code
    # Fallback: return the first change's code if there's only one
    if len(changes) == 1:
        return changes[0].code
    return None


def _extract_tests(test_files: list, filename: str) -> Optional[str]:
    """Extract test code for a specific file from a list of GeneratedTestFile objects."""
    for tf in test_files:
        if tf.filepath == filename:
            return tf.tests
    if len(test_files) == 1:
        return test_files[0].tests
    return None


def _build_failure_message_multi(eval_result, test_files: dict) -> str:
    """Build failure message including all test file contents for multi-file mode."""
    parts = []
    if eval_result.error:
        parts.append(f"Error: {eval_result.error.type}: {eval_result.error.message}")
        if eval_result.error.traceback:
            parts.append(eval_result.error.traceback)
            if eval_result.stdout and eval_result.stdout != eval_result.error.traceback:
                parts.append(f"stdout:\n{eval_result.stdout}")
        elif eval_result.stdout:
            parts.append(f"stdout:\n{eval_result.stdout}")
    elif eval_result.stdout:
        parts.append(f"stdout:\n{eval_result.stdout}")
    if eval_result.stderr:
        parts.append(f"stderr:\n{eval_result.stderr}")
    if test_files:
        parts.append("\n\nTEST FILES (the tests your code must pass):")
        for fp, tests in test_files.items():
            parts.append(f"── {fp} ──\n{tests}")
    return "\n".join(parts) or "Tests failed with no additional output."


def _build_failure_message(eval_result, test_code: str = None) -> str:
    parts = []
    if eval_result.error:
        parts.append(f"Error: {eval_result.error.type}: {eval_result.error.message}")
        if eval_result.error.traceback:
            parts.append(eval_result.error.traceback)
            # For test failures, error.traceback already contains the full
            # pytest stdout.  Only add stdout separately when it differs
            # (e.g. internal errors) to avoid duplicating the same output.
            if eval_result.stdout and eval_result.stdout != eval_result.error.traceback:
                parts.append(f"stdout:\n{eval_result.stdout}")
        elif eval_result.stdout:
            parts.append(f"stdout:\n{eval_result.stdout}")
    elif eval_result.stdout:
        parts.append(f"stdout:\n{eval_result.stdout}")
    if eval_result.stderr:
        parts.append(f"stderr:\n{eval_result.stderr}")
    if test_code:
        parts.append(f"\n\nTEST CODE (the tests your code must pass):\n──────────────────────────────────────────────────────────────\n{test_code}")
    return "\n".join(parts) or "Tests failed with no additional output."
