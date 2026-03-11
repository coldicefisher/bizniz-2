"""
CodingOrchestrator

Iteratively generates code (via Autocoder) and tests (via Autotester), runs the
tests, and repairs the code on failure until the tests pass or safeguards trigger.

Strategies
----------
- TDD (default): generate tests first from the spec, then code to pass them.
  Debug loop fixes code only — tests are the source of truth.
- CODE_FIRST: generate code first, then tests. Debug loop can fix either.
  Used as a fallback when TDD fails.

Iteration flow (TDD)
---------------------
1.  Autotester.generate_multi(prompt) → contract tests (no source code)
2.  Autocoder.generate_multi(prompt + test_code) → generate code to pass tests
3.  DockerPytestEnvironment.execute(test_file) → run tests inside Docker
4.  If tests pass → done
5.  If tests fail → repair code (tests are the spec, not modified)
6.  Repeat 3-5 until success or safeguards fire

Safeguards
----------
- Stale loop: same code hash on two consecutive iterations → escalate model
- Model escalation: on stalls, switch to a stronger model
- Max iterations cap → OrchestratorMaxIterationsError
"""

import hashlib
import re
import sys
import time
from pathlib import Path
from typing import Optional, Callable, List, Set

from bizniz.autocoder.autocoder import Autocoder
from bizniz.autodebugger.autodebugger import Autodebugger
from bizniz.agentic_debugger.agentic_debugger import AgenticDebugger
from bizniz.autotester.autotester import Autotester
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import ExecutionCallSpec
from bizniz.orchestrator.model_progression import ModelProgression
from bizniz.orchestrator.stall_detector import StallDetector
from bizniz.workspace.base_workspace import BaseWorkspace

from bizniz.clients.errors import AIInsufficientFunds
from bizniz.autocoder.types import FileChange
from bizniz.autotester.types import GeneratedTestFile
from bizniz.orchestrator.strategy import CodingStrategy
from bizniz.preflight.registry import get_validator
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

    # Hard cap on total collection error regeneration cycles across all resets
    MAX_TOTAL_COLLECTION_ERRORS = 12

    # Max times we'll try to install the same package before giving up
    MAX_PACKAGE_INSTALL_ATTEMPTS = 3

    # Wall-clock timeout for a single run_multi call (seconds)
    WALL_CLOCK_TIMEOUT = 1800  # 30 minutes

    def __init__(
        self,
        autocoder: Autocoder,
        autotester: Autotester,
        test_environment: BaseExecutionEnvironment,
        workspace: BaseWorkspace,
        autodebugger: Optional[Autodebugger] = None,
        client: Optional[BaseAIClient] = None,
        client_factory: Optional[Callable[[str], BaseAIClient]] = None,
        debugger_factory: Optional[Callable[[], AgenticDebugger]] = None,
        model_progression: Optional[ModelProgression] = None,
        autocoder_progression: Optional[ModelProgression] = None,
        autotester_progression: Optional[ModelProgression] = None,
        repair_progression: Optional[ModelProgression] = None,
        stall_threshold: int = 2,
        agentic_debug_threshold: int = 2,
        max_iterations: int = 20,
        on_status_message: Optional[Callable[[str], None]] = None,
        language: str = "python",
    ):
        self._autocoder = autocoder
        self._autotester = autotester
        self._autodebugger = autodebugger
        self._test_environment = test_environment
        self._workspace = workspace
        self._client = client
        self._client_factory = client_factory
        self._debugger_factory = debugger_factory
        # Per-agent progressions (fall back to shared model_progression)
        self._model_progression = model_progression
        self._autocoder_progression = autocoder_progression or model_progression
        self._autotester_progression = autotester_progression or model_progression
        self._repair_progression = repair_progression or model_progression
        self._stall_threshold = stall_threshold
        self._agentic_debug_threshold = agentic_debug_threshold
        self._max_iterations = max_iterations
        self._on_status_message = on_status_message
        self._language = language
        self._stall_detector = StallDetector(
            consecutive_fail_threshold=stall_threshold,
        )
        self._stall_cycle_count = 0
        self._editable_install_failed = False  # skip pip install -e . after first failure
        self._test_scaffold = ""  # cached scaffold from autocoder for test regeneration

        # Override system prompts for non-Python languages
        if language == "typescript":
            self._apply_typescript_system_prompts()

    def _apply_typescript_system_prompts(self):
        """Override autocoder/autotester system prompts for TypeScript."""
        from bizniz.autocoder.prompts.generate_multi_prompt import get_generate_multi_system_prompt as get_autocoder_prompt
        from bizniz.autotester.prompts.generate_multi_prompt import get_generate_multi_system_prompt as get_autotester_prompt

        autocoder_prompt = get_autocoder_prompt("typescript")
        if hasattr(self._test_environment, 'describe'):
            autocoder_prompt = autocoder_prompt.format(evaluation_environment=self._test_environment.describe())
        self._autocoder.set_system_prompt_override(autocoder_prompt)

        autotester_prompt = get_autotester_prompt("typescript")
        self._autotester.set_system_prompt_override(autotester_prompt)

    # ── Language helpers ──────────────────────────────────────────────────────

    @property
    def _test_symbol(self) -> str:
        return "jest" if self._language == "typescript" else "pytest"

    @property
    def _code_fence_lang(self) -> str:
        return "typescript" if self._language == "typescript" else "python"

    @property
    def _language_prefix(self) -> str:
        if self._language == "typescript":
            return (
                "IMPORTANT: This is a TypeScript project. "
                "All source files must use .ts or .tsx extensions. "
                "All test files must end in .test.ts or .test.tsx (Jest convention). "
                "Use ES module imports. Do NOT generate Python code.\n\n"
            )
        return ""

    def _is_test_file(self, filepath: str) -> bool:
        if self._language == "typescript":
            return (
                not filepath.startswith("node_modules/")
                and (
                    filepath.endswith(".test.ts")
                    or filepath.endswith(".test.tsx")
                    or filepath.endswith(".spec.ts")
                    or filepath.endswith(".spec.tsx")
                )
            )
        return (
            filepath.startswith("tests/")
            and filepath.endswith(".py")
            and filepath != "tests/__init__.py"
        )

    def _strip_extension(self, filepath: str) -> str:
        if self._language == "typescript":
            for ext in (".test.tsx", ".test.ts", ".spec.tsx", ".spec.ts", ".tsx", ".ts"):
                if filepath.endswith(ext):
                    return filepath[:-len(ext)]
            return filepath
        return filepath.replace(".py", "")

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(
        self,
        prompt: str,
        code_filename: str,
        test_filename: str,
        strategy: CodingStrategy = CodingStrategy.TDD,
    ) -> OrchestratorResult:
        """
        Run the full iterative coding + testing loop (single-file).
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        self._strategy = strategy

        # Load persisted packages from workspace DB into the Docker environment
        self._sync_environment_packages(log)

        previous_code_hash: Optional[str] = None
        current_code: Optional[str] = None
        stale_count: int = 0
        _attempted_packages: dict = {}  # package_name -> attempt count

        if strategy == CodingStrategy.TDD:
            # TDD: tests first, then code to pass them
            log("Orchestrator [TDD]: generating contract tests from spec...")
            test_result = self._autotester.process_from_prompt(
                prompt=prompt,
                output_path=test_filename,
                code_filename=code_filename,
            )
            current_tests = _extract_tests(test_result.test_files, test_filename)

            log("Orchestrator [TDD]: generating code to pass tests...")
            test_context = f"\n\nYOUR CODE MUST PASS THESE TESTS:\n```python\n{current_tests}\n```"
            code_result = self._autocoder.generate_only(
                prompt=prompt + test_context,
                filename=code_filename,
            )
            current_code = _extract_code(code_result.changes, code_filename) or self._workspace.read_file(code_filename)
        else:
            # Code-first: code then tests
            log("Orchestrator [CODE_FIRST]: generating initial code...")
            code_result = self._autocoder.generate_only(
                prompt=prompt,
                filename=code_filename,
            )
            current_code = _extract_code(code_result.changes, code_filename) or self._workspace.read_file(code_filename)

            log("Orchestrator [CODE_FIRST]: generating contract tests...")
            test_result = self._autotester.process_from_prompt(
                prompt=prompt,
                output_path=test_filename,
                code_filename=code_filename,
            )
            current_tests = _extract_tests(test_result.test_files, test_filename)

        # ── Proactive package installation ─────────────────────────────────────
        code_dict = {code_filename: current_code} if current_code else {}
        test_dict = {test_filename: current_tests} if current_tests else {}
        self._proactive_package_install(code_dict, test_dict, log)
        self._install_project_editable(log)

        # ── Test run + repair loop ─────────────────────────────────────────────
        for iteration in range(1, self._max_iterations + 1):
            log(f"Orchestrator: running tests (iteration {iteration}/{self._max_iterations})...")

            test_abs_path = str(self._workspace.path(test_filename))
            call_spec = ExecutionCallSpec(symbol=self._test_symbol, args=[test_abs_path])
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
                    _attempted_packages[missing_pkg] = _attempted_packages.get(missing_pkg, 0) + 1
                    if _attempted_packages[missing_pkg] <= self.MAX_PACKAGE_INSTALL_ATTEMPTS:
                        log(f"Orchestrator: detected missing package '{missing_pkg}', installing...")
                        self._install_package(missing_pkg, log)
                        continue  # Re-run tests without counting as a failed iteration
                    else:
                        log(f"Orchestrator: package '{missing_pkg}' still missing after "
                            f"{self.MAX_PACKAGE_INSTALL_ATTEMPTS} install attempts, proceeding to repair...")

            # ── Collection error → regenerate tests ───────────────────────────
            is_collection_error = (
                eval_result.error
                and eval_result.error.message
                and ("exited with code 2" in eval_result.error.message
                     or "exited with code 4" in eval_result.error.message)
            )
            if is_collection_error:
                log("Orchestrator: test collection error — regenerating tests...")
                error_detail = ""
                if eval_result.error and eval_result.error.traceback:
                    error_detail = eval_result.error.traceback
                elif eval_result.stdout:
                    error_detail = eval_result.stdout
                if not error_detail and eval_result.stderr:
                    error_detail = eval_result.stderr
                elif eval_result.stderr and eval_result.stderr not in error_detail:
                    error_detail += f"\nstderr: {eval_result.stderr}"

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
            try:
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

                # ── Heuristic fallback (no autodebugger) ──────────────────────
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
            except AIInsufficientFunds:
                raise
            except Exception as e:
                log(f"Orchestrator: repair failed ({type(e).__name__}: {e}), retrying...")

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
        initial_model: Optional[str] = None,
        strategy: CodingStrategy = CodingStrategy.TDD,
        workspace_context: Optional[dict] = None,
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
        strategy:
            CodingStrategy.TDD (default) — tests first, fix code only.
            CodingStrategy.CODE_FIRST — code first, fix either.
        workspace_context:
            Optional dict of existing workspace files {filepath: content}
            for cross-issue learning.
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        self._strategy = strategy
        log(f"Orchestrator: using {strategy.value} strategy")

        # Set starting model if suggested by the engineer
        if initial_model and self._model_progression and self._client:
            self._model_progression.set_start(initial_model)
            if self._autocoder_progression and self._autocoder_progression is not self._model_progression:
                self._autocoder_progression.set_start(initial_model)
            if self._autotester_progression and self._autotester_progression is not self._model_progression:
                self._autotester_progression.set_start(initial_model)
            if self._repair_progression and self._repair_progression is not self._model_progression:
                self._repair_progression.set_start(initial_model)
            current = self._model_progression.current_model
            if self._client_factory:
                fresh_client = self._client_factory(current)
                self._client = fresh_client
                self._autocoder._client = fresh_client
                self._autotester._client = fresh_client
                if self._autodebugger:
                    self._autodebugger._client = fresh_client
            else:
                self._client.set_model(current)
            log(f"Orchestrator: starting with suggested model {current}")

        self._sync_environment_packages(log)

        stale_count = 0
        previous_code_hash: Optional[str] = None
        collection_error_count = 0
        total_collection_errors = 0
        regression_count = 0
        max_regression_retries = 3
        self._stall_cycle_count = 0
        last_failure_output = ""
        _attempted_packages: dict = {}  # package_name -> attempt count
        _wall_clock_start = time.time()

        # ── Load existing code for files being modified ──────────────────────
        existing_code = {}
        for tf in target_files:
            if tf.get("action") == "modify" and self._workspace.exists(path=tf["filepath"]):
                existing_code[tf["filepath"]] = self._workspace.read_file(path=tf["filepath"])

        # ── Snapshot passing tests before we start (regression baseline) ─────
        baseline_passing = self._get_passing_tests(log)

        # ── Build workspace context for cross-issue learning ─────────────────
        extra_context = ""
        if workspace_context:
            ctx_parts = []
            for fp, content in workspace_context.items():
                ctx_parts.append(f"── {fp} ──\n```{self._code_fence_lang}\n{content}\n```")
            extra_context = (
                "\n\nEXISTING CODEBASE (from previously resolved issues):\n"
                + "\n\n".join(ctx_parts) + "\n"
            )

        # ── Auto-build workspace manifest ────────────────────────────────────
        # Lightweight summary of existing files so LLM doesn't waste turns exploring
        manifest = self._build_workspace_manifest()
        if manifest:
            extra_context += f"\n\nWORKSPACE MANIFEST (existing files and their exports):\n{manifest}\n"

        # ── Enrich test setup hints from actual workspace ────────────────────
        # Scans disk for app factories and routers, appending verified import
        # paths so the autotester uses real imports instead of guessing.
        prompt = self._enrich_test_setup_hint(prompt, test_files, architecture_context, log)

        if strategy == CodingStrategy.TDD:
            current_files, current_test_files = self._generate_tdd(
                prompt, target_files, test_files, architecture_context,
                existing_code, extra_context, log,
            )
        else:
            current_files, current_test_files = self._generate_code_first(
                prompt, target_files, test_files, architecture_context,
                existing_code, extra_context, log,
            )

        # ── Pre-flight validation ─────────────────────────────────────────────
        # Check import resolution and auto-stub missing modules before tests
        current_files = self._run_preflight(
            current_files, self._get_installed_packages(), log,
        )

        # ── Proactive package installation ───────────────────────────────────
        self._proactive_package_install(current_files, current_test_files, log)

        # ── Install project in editable mode if pyproject.toml/setup.py exists ─
        self._install_project_editable(log)

        # ── Test + repair loop ───────────────────────────────────────────────
        for iteration in range(1, self._max_iterations + 1):
            # Wall-clock timeout check
            elapsed_wall = time.time() - _wall_clock_start
            if elapsed_wall > self.WALL_CLOCK_TIMEOUT:
                log(f"Orchestrator: wall-clock timeout ({elapsed_wall:.0f}s > {self.WALL_CLOCK_TIMEOUT}s)")
                raise OrchestratorMaxIterationsError(
                    f"Wall-clock timeout after {elapsed_wall:.0f}s "
                    f"({iteration - 1} iterations completed)."
                )
            log(f"Orchestrator: running tests (iteration {iteration}/{self._max_iterations})...")

            test_abs_paths = [
                str(self._workspace.path(tf)) for tf in current_test_files.keys()
            ]
            call_spec = ExecutionCallSpec(symbol=self._test_symbol, args=test_abs_paths)
            t0 = time.time()
            eval_result = self._test_environment.execute(code="", call_spec=call_spec)
            test_elapsed = time.time() - t0
            log(f"Orchestrator: tests {'PASSED' if eval_result.success else 'FAILED'} in {test_elapsed:.1f}s")

            if eval_result.success:
                # Check for regressions against baseline
                regressions = self._detect_regressions(baseline_passing, log)

                # Check for architecture drift
                actual_changes = [
                    FileChange(filepath=fp, code=code, action="create")
                    for fp, code in current_files.items()
                ]
                drift = self._detect_drift(target_files, actual_changes)

                if regressions:
                    regression_count += 1
                    log(f"Orchestrator: {len(regressions)} regression(s) detected: {regressions}")

                    # After max regression retries, accept the result — issue's own tests pass
                    if regression_count > max_regression_retries:
                        log(f"Orchestrator: accepting result after {max_regression_retries} regression repair attempts "
                            f"(issue tests pass, {len(regressions)} regression(s) remain)")
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

                    # Include regressing test content so LLM knows what must still pass
                    regression_details = []
                    for reg_path in regressions:
                        try:
                            content = self._workspace.read_file(path=reg_path)
                            regression_details.append(f"── {reg_path} ──\n{content}")
                        except Exception:
                            regression_details.append(f"── {reg_path} ── (could not read)")

                    failure_output = (
                        f"Tests passed but REGRESSIONS detected in: {', '.join(regressions)}\n"
                        f"These tests were passing before your changes and now fail.\n"
                        f"Fix the code to make all tests pass without breaking existing functionality.\n\n"
                        f"REGRESSING TEST FILES:\n" + "\n\n".join(regression_details)
                    )
                    try:
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
                    except AIInsufficientFunds:
                        raise
                    except Exception as e:
                        log(f"Orchestrator: regression repair failed ({type(e).__name__}: {e}), retrying...")
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
            last_failure_output = failure_output
            # Log failure detail (truncated) for debugging
            fail_preview = (failure_output or "")[:200].replace("\n", " | ")
            log(f"Orchestrator: tests failed (iteration {iteration}): {fail_preview}")

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
                    _attempted_packages[missing_pkg] = _attempted_packages.get(missing_pkg, 0) + 1
                    if _attempted_packages[missing_pkg] <= self.MAX_PACKAGE_INSTALL_ATTEMPTS:
                        log(f"Orchestrator: installing missing package '{missing_pkg}'...")
                        self._install_package(missing_pkg, log)
                        continue
                    else:
                        log(f"Orchestrator: package '{missing_pkg}' still missing after "
                            f"{self.MAX_PACKAGE_INSTALL_ATTEMPTS} install attempts, proceeding to repair...")

            # ── Collection error → regenerate tests ───────────────────────
            is_collection_error = (
                eval_result.error
                and eval_result.error.message
                and ("exited with code 2" in eval_result.error.message
                     or "exited with code 4" in eval_result.error.message)
            )
            if is_collection_error:
                collection_error_count += 1
                total_collection_errors += 1

                # Hard cap: if we've burned too many iterations on collection errors, give up
                if total_collection_errors >= self.MAX_TOTAL_COLLECTION_ERRORS:
                    log(f"Orchestrator: {total_collection_errors} total collection errors — giving up")
                    raise OrchestratorMaxIterationsError(
                        f"Too many test collection errors ({total_collection_errors}). "
                        f"Tests cannot be collected — likely a structural issue."
                    )

                error_detail = ""
                if eval_result.error and eval_result.error.traceback:
                    error_detail = eval_result.error.traceback
                elif eval_result.stdout:
                    error_detail = eval_result.stdout
                # Also check stderr (exit code 4 often only has stderr)
                if not error_detail and eval_result.stderr:
                    error_detail = eval_result.stderr
                elif eval_result.stderr and eval_result.stderr not in error_detail:
                    error_detail += f"\nstderr: {eval_result.stderr}"

                log(f"Orchestrator: collection error detail: {error_detail[:500]}")

                # Auto-fix bad pytest config files created by debugger
                fixed_config = self._fix_bad_pytest_config(current_files, error_detail, log)
                if fixed_config:
                    log("Orchestrator: removed bad pytest config — retrying...")
                    collection_error_count = 0
                    continue

                # Auto-fix file/directory collisions (e.g. models.py vs models/)
                fixed_collision = self._fix_file_directory_collisions(current_files, log)
                if fixed_collision:
                    log("Orchestrator: fixed file/directory collision — retrying...")
                    collection_error_count = 0
                    continue

                # If collection error is an ImportError in source code (not tests),
                # repair the source code instead of just regenerating tests
                if "ImportError" in error_detail and collection_error_count <= 2:
                    # Check if the import error traces back to a source file (not a test file)
                    source_import_error = False
                    for fp in current_files:
                        basename = fp.rsplit("/", 1)[-1] if "/" in fp else fp
                        module = basename.replace(".py", "")
                        if basename in error_detail or module in error_detail:
                            source_import_error = True
                            break
                    if source_import_error:
                        log("Orchestrator: collection error caused by source code import — repairing code...")
                        all_files = {**current_files}
                        for fp, tests in current_test_files.items():
                            all_files[fp] = tests
                        try:
                            repaired = self._autocoder.repair_multi(
                                current_files=all_files,
                                error_message=(
                                    f"Test collection failed with ImportError. "
                                    f"IMPORTANT: Trace the FULL import chain — the error may be in a transitive dependency, "
                                    f"not the file directly mentioned. Read each imported module to find the broken link.\n\n"
                                    f"FULL ERROR:\n{error_detail}"
                                ),
                                architecture_context=architecture_context,
                            )
                            if repaired.dependencies:
                                self._install_declared_dependencies(repaired.dependencies, log)
                            for ch in repaired.changes:
                                if ch.filepath in current_test_files:
                                    current_test_files[ch.filepath] = ch.code
                                else:
                                    current_files[ch.filepath] = ch.code
                                    self._workspace.write_file(path=ch.filepath, content=ch.code)
                            # Re-install project after code changes
                            self._install_project_editable(log)
                            stale_count = 0
                            previous_code_hash = None
                            continue
                        except AIInsufficientFunds:
                            raise
                        except Exception as e:
                            log(f"Orchestrator: code repair for import error failed ({type(e).__name__}: {e})")

                # After 3 consecutive collection errors, escalate model and clear history
                if collection_error_count >= self._stall_threshold:
                    escalated = self._try_escalate_model(log, progression=self._repair_progression, agent="repair")
                    if escalated:
                        log("Orchestrator: repeated collection errors — escalating model...")
                    collection_error_count = 0

                    # If escalation failed (models exhausted) AND we've had many total
                    # collection errors, use debugger to diagnose the structural issue
                    # and regenerate code from scratch
                    has_debugger = self._debugger_factory is not None
                    if not escalated and total_collection_errors >= 3 and has_debugger:
                        try:
                            log("Orchestrator: persistent collection errors — running diagnosis...")
                            self._autocoder.clear_message_history()
                            self._autotester.clear_message_history()

                            diag_text = ""
                            diag_fix_plan = []
                            diag_approach = ""
                            diag_code_fixes = []

                            if self._debugger_factory is not None:
                                debugger = self._debugger_factory()
                                ad = debugger.diagnose(
                                    error_output=error_detail,
                                    source_files=current_files,
                                    test_files=current_test_files,
                                )
                                log(f"Orchestrator: agentic diagnosis — {ad.root_cause_category}, "
                                    f"fix_target={ad.fix_target}")
                                diag_text = ad.diagnosis
                                diag_fix_plan = ad.fix_plan
                                diag_approach = ad.suggested_approach
                                diag_code_fixes = ad.code_fixes

                            # If agentic debugger produced direct fixes, apply them
                            if diag_code_fixes:
                                log(f"Orchestrator: applying {len(diag_code_fixes)} direct fix(es) for collection errors...")
                                for fix in diag_code_fixes:
                                    if fix.filepath in current_test_files:
                                        current_test_files[fix.filepath] = fix.new_content
                                    else:
                                        current_files[fix.filepath] = fix.new_content
                                        self._workspace.write_file(path=fix.filepath, content=fix.new_content)
                                stale_count = 0
                                previous_code_hash = None
                                continue

                            # Regenerate code with diagnosis context, then fresh tests
                            log("Orchestrator: regenerating code and tests from scratch with diagnosis...")
                            enriched_code_prompt = (
                                f"{prompt}\n\n"
                                f"DIAGNOSIS OF PERSISTENT FAILURE:\n{diag_text}\n\n"
                                f"FIX PLAN:\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(diag_fix_plan)) + "\n\n"
                                f"APPROACH: {diag_approach}\n\n"
                                f"The previous code produced tests that could not even be collected by pytest.\n"
                                f"Collection error:\n{error_detail}\n"
                            )
                            code_result = self._autocoder.generate_multi(
                                issue_description=enriched_code_prompt,
                                target_files=target_files,
                                architecture_context=architecture_context,
                                existing_code=existing_code,
                            )
                            current_files = {ch.filepath: ch.code for ch in code_result.changes}
                            test_result = self._autotester.generate_multi(
                                problem_statement=prompt + self._get_scaffold_context(),
                                test_files=list(current_test_files.keys()),
                                source_code=current_files,
                                architecture_context=architecture_context,
                            )
                            current_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}
                            stale_count = 0
                            previous_code_hash = None
                            continue
                        except AIInsufficientFunds:
                            raise
                        except Exception as e:
                            log(f"Orchestrator: diagnosis regeneration failed ({type(e).__name__}: {e}), continuing...")

                # Clear autotester history to prevent token bloat
                self._autotester.clear_message_history()

                log("Orchestrator: test collection error — regenerating tests...")
                enriched_prompt = (
                    f"{prompt}{self._get_scaffold_context()}\n\n"
                    f"CURRENT CODE:\n"
                    + "\n".join(f"── {fp} ──\n```{self._code_fence_lang}\n{code}\n```" for fp, code in current_files.items())
                    + f"\n\nThe previous tests had collection errors:\n{error_detail}\n"
                    f"Fix the imports and test structure."
                )
                try:
                    test_result = self._autotester.generate_multi(
                        problem_statement=enriched_prompt,
                        test_files=list(current_test_files.keys()),
                        source_code=current_files,
                        architecture_context=architecture_context,
                    )
                    current_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}
                except AIInsufficientFunds:
                    raise
                except Exception as e:
                    log(f"Orchestrator: test regeneration failed ({type(e).__name__}: {e}), retrying...")
                stale_count = 0
                previous_code_hash = None
                continue

            # ── Repair ────────────────────────────────────────────────────
            try:
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
            except AIInsufficientFunds:
                raise
            except Exception as e:
                log(f"Orchestrator: repair failed ({type(e).__name__}: {e}), retrying...")

        raise OrchestratorMaxIterationsError(
            f"Failed to produce passing tests after {self._max_iterations} iterations."
        )

    # ── Generation strategies ─────────────────────────────────────────────────

    def _generate_tdd(
        self, prompt, target_files, test_files, architecture_context,
        existing_code, extra_context, log,
    ):
        """TDD: generate tests per file (no source code), then code per file."""
        # Step 1: Generate tests one file at a time from spec only
        log(f"Orchestrator [TDD]: generating {len(test_files)} test file(s) from spec...")
        t0 = time.time()
        test_examples = self._get_passing_test_examples()
        test_prompt = self._language_prefix + prompt + extra_context + test_examples
        current_test_files = {}
        all_deps = []
        for test_fp in test_files:
            test_result = self._autotester.generate_multi(
                problem_statement=test_prompt,
                test_files=[test_fp],
                source_code=None,  # TDD: no source code, tests define the contract
            )
            for tf in test_result.test_files:
                current_test_files[tf.filepath] = tf.tests
            all_deps.extend(test_result.dependencies)
        log(f"Orchestrator [TDD]: test generation done in {time.time() - t0:.1f}s")

        # Step 2: Generate code one file at a time to pass the tests
        log(f"Orchestrator [TDD]: generating code for {len(target_files)} file(s) to pass tests...")
        t0 = time.time()
        current_files = {}
        ordered_targets = _order_by_dependencies(target_files)
        for tf in ordered_targets:
            fp = tf["filepath"]
            # Find the test file(s) relevant to this source file
            related_tests = _find_tests_for_source(fp, current_test_files)
            test_context = "\n\n".join(
                f"── {tfp} ──\n```{self._code_fence_lang}\n{tests}\n```"
                for tfp, tests in related_tests.items()
            )
            ctx_code = dict(current_files)
            if fp in existing_code:
                ctx_code[fp] = existing_code[fp]
            code_prompt = (
                f"{self._language_prefix}{prompt}{extra_context}\n\n"
                f"YOUR CODE MUST PASS THESE TESTS:\n{test_context}"
            )
            code_result = self._autocoder.generate_multi(
                issue_description=code_prompt,
                target_files=[tf],
                architecture_context=architecture_context,
                existing_code=ctx_code,
            )
            for ch in code_result.changes:
                current_files[ch.filepath] = ch.code
            all_deps.extend(code_result.dependencies)
        log(f"Orchestrator [TDD]: code generation done in {time.time() - t0:.1f}s ({len(current_files)} files)")

        # Install LLM-declared dependencies
        if all_deps:
            self._install_declared_dependencies(list(set(all_deps)), log)

        return current_files, current_test_files

    def _generate_code_first(
        self, prompt, target_files, test_files, architecture_context,
        existing_code, extra_context, log,
    ):
        """Code-first: generate code per file, then tests per file."""
        # Step 1: Generate code one file at a time in dependency order
        log(f"Orchestrator [CODE_FIRST]: generating code for {len(target_files)} file(s)...")
        t0 = time.time()
        current_files = {}
        all_deps = []
        test_scaffold = ""
        ordered_targets = _order_by_dependencies(target_files)
        for tf in ordered_targets:
            # Context: only already-generated files + existing code for this file
            ctx_code = dict(current_files)
            fp = tf["filepath"]
            if fp in existing_code:
                ctx_code[fp] = existing_code[fp]
            code_result = self._autocoder.generate_multi(
                issue_description=self._language_prefix + prompt + extra_context,
                target_files=[tf],
                architecture_context=architecture_context,
                existing_code=ctx_code,
            )
            for ch in code_result.changes:
                current_files[ch.filepath] = ch.code
            all_deps.extend(code_result.dependencies)
            # Capture test scaffold from autocoder (last non-empty one wins)
            if code_result.test_scaffold:
                test_scaffold = code_result.test_scaffold
                self._test_scaffold = test_scaffold
        log(f"Orchestrator [CODE_FIRST]: code generation done in {time.time() - t0:.1f}s ({len(current_files)} files)")

        # Step 2: Generate tests one file at a time, sending only the relevant source
        log(f"Orchestrator [CODE_FIRST]: generating {len(test_files)} test file(s)...")
        t0 = time.time()
        current_test_files = {}
        test_examples = self._get_passing_test_examples()
        scaffold_context = ""
        if test_scaffold:
            scaffold_context = (
                f"\n\nTEST SCAFFOLD (from the code author — use this as your starting point):\n"
                f"```{self._code_fence_lang}\n{test_scaffold}\n```\n"
            )
        for test_fp in test_files:
            related_source = _find_source_for_test(test_fp, current_files)
            test_prompt = self._language_prefix + prompt + extra_context + test_examples + scaffold_context
            test_result = self._autotester.generate_multi(
                problem_statement=test_prompt,
                test_files=[test_fp],
                source_code=related_source,
            )
            for tf in test_result.test_files:
                current_test_files[tf.filepath] = tf.tests
            all_deps.extend(test_result.dependencies)
        log(f"Orchestrator [CODE_FIRST]: test generation done in {time.time() - t0:.1f}s")

        # Install LLM-declared dependencies
        if all_deps:
            self._install_declared_dependencies(list(set(all_deps)), log)

        return current_files, current_test_files

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
        Handle a test failure in multi-file mode.

        Uses StallDetector to track multiple stall signals. When stalled,
        runs AgenticDebugger for comprehensive diagnosis before escalating.

        Returns (current_files, current_test_files, stale_count, previous_code_hash).
        """
        # Record failure in stall detector
        combined = "".join(sorted(f"{k}:{v}" for k, v in current_files.items()))
        current_hash = _hash(combined)
        self._stall_detector.record_failure(current_hash, failure_output)

        if self._stall_detector.is_stalled:
            log(f"Orchestrator: stall detected — {self._stall_detector.stall_reason}")
            self._stall_cycle_count += 1

            # Agentic diagnosis — only if we've hit the agentic debug threshold
            agentic_diagnosis = None
            if (self._debugger_factory is not None
                    and self._stall_detector._consecutive_failures >= self._agentic_debug_threshold):
                try:
                    log("Orchestrator: running agentic debugger...")
                    debugger = self._debugger_factory()
                    agentic_diagnosis = debugger.diagnose(
                        error_output=failure_output,
                        source_files=current_files,
                        test_files=current_test_files,
                        repair_history=self._stall_detector.repair_history,
                    )
                    log(f"Orchestrator: agentic diagnosis — {agentic_diagnosis.root_cause_category}, "
                        f"fix_target={agentic_diagnosis.fix_target} "
                        f"(confidence: {agentic_diagnosis.confidence})")
                except AIInsufficientFunds:
                    raise
                except Exception as e:
                    log(f"Orchestrator: agentic debugger failed ({type(e).__name__}: {e}), proceeding...")

            # Normalize diagnosis into a unified view
            diagnosis = agentic_diagnosis
            diagnosis_text = ""
            missing_packages = []
            fix_target = None
            fix_plan = []
            suggested_approach = ""
            code_fixes = []

            if agentic_diagnosis:
                diagnosis_text = agentic_diagnosis.diagnosis
                missing_packages = agentic_diagnosis.missing_packages
                fix_target = agentic_diagnosis.fix_target
                fix_plan = agentic_diagnosis.fix_plan
                suggested_approach = agentic_diagnosis.suggested_approach
                code_fixes = agentic_diagnosis.code_fixes

            # Install missing packages if identified
            if missing_packages:
                for pkg in missing_packages:
                    log(f"Orchestrator: diagnosis identified missing package '{pkg}', installing...")
                    self._install_package(pkg, log)

            # Escalate repair model
            self._try_escalate_model(log, progression=self._repair_progression, agent="repair")
            self._stall_detector.reset_counters()

            # If the issue was purely a dependency problem, re-run without repair
            if (diagnosis
                    and diagnosis.root_cause_category == "dependency_issue"
                    and missing_packages):
                log("Orchestrator: dependency issue resolved — retrying tests...")
                return current_files, current_test_files, 0, None

            # Clear message histories to prevent token bloat after escalation
            self._autocoder.clear_message_history()
            self._autotester.clear_message_history()

            # Strategy flip: after 2 stall cycles in TDD, switch to code-first
            # (and vice versa). This lets the agentic debugger modify tests too.
            is_tdd = getattr(self, '_strategy', None) == CodingStrategy.TDD
            if self._stall_cycle_count == 2 and is_tdd:
                log("Orchestrator: flipping from TDD to CODE_FIRST — tests can now be modified")
                self._strategy = CodingStrategy.CODE_FIRST
                # Regenerate tests from spec only (no source blob — architecture context is in the prompt)
                test_result = self._autotester.generate_multi(
                    problem_statement=prompt + self._get_scaffold_context() + f"\n\nPrevious failures:\n{failure_output}",
                    test_files=test_files,
                    source_code=None,
                )
                new_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}
                return current_files, new_test_files, 0, None
            elif self._stall_cycle_count == 2 and not is_tdd:
                log("Orchestrator: flipping from CODE_FIRST to TDD — tests become the spec")
                self._strategy = CodingStrategy.TDD
                # Regenerate tests from spec (TDD style, no source code)
                test_result = self._autotester.generate_multi(
                    problem_statement=prompt + self._get_scaffold_context(),
                    test_files=test_files,
                    source_code=None,
                )
                new_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}
                # Regenerate code to pass the new tests — feed test files one at a time
                # to avoid a massive token blob
                new_files = {}
                for fp, t in new_test_files.items():
                    test_context = f"── {fp} ──\n```{self._code_fence_lang}\n{t}\n```"
                    code_result = self._autocoder.generate_multi(
                        issue_description=(
                            f"{prompt}\n\nYOUR CODE MUST PASS THIS TEST:\n{test_context}"
                        ),
                        target_files=target_files,
                        architecture_context=architecture_context,
                        existing_code=new_files,
                    )
                    for ch in code_result.changes:
                        new_files[ch.filepath] = ch.code
                return new_files, new_test_files, 0, None

            # After multiple stall cycles without progress, do a full regeneration
            if self._stall_cycle_count >= 3:
                log(f"Orchestrator: {self._stall_cycle_count} stall cycles — regenerating code and tests from scratch...")
                self._stall_cycle_count = 0
                diagnosis_context = ""
                if diagnosis_text:
                    diagnosis_context = (
                        f"\n\nDIAGNOSIS OF PERSISTENT FAILURE:\n{diagnosis_text}\n\n"
                        f"FIX PLAN:\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(fix_plan)) + "\n\n"
                        f"APPROACH: {suggested_approach}\n"
                    )
                enriched_code_prompt = (
                    f"{prompt}{diagnosis_context}\n\n"
                    f"Previous failures after multiple diagnosis rounds:\n{failure_output}\n"
                    f"Generate a COMPLETELY FRESH implementation — do NOT repeat the same approach."
                )
                code_result = self._autocoder.generate_multi(
                    issue_description=enriched_code_prompt,
                    target_files=target_files,
                    architecture_context=architecture_context,
                    existing_code={},
                )
                new_files = {ch.filepath: ch.code for ch in code_result.changes}
                # Update scaffold from fresh code generation
                if code_result.test_scaffold:
                    self._test_scaffold = code_result.test_scaffold
                test_result = self._autotester.generate_multi(
                    problem_statement=prompt + self._get_scaffold_context(),
                    test_files=test_files,
                    source_code=new_files,
                )
                new_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}
                return new_files, new_test_files, 0, None

            # If agentic debugger produced direct code fixes, apply them
            if code_fixes:
                log(f"Orchestrator: applying {len(code_fixes)} direct code fix(es) from agentic debugger...")
                new_files = dict(current_files)
                new_test_files = dict(current_test_files)
                for fix in code_fixes:
                    if fix.filepath in new_test_files:
                        new_test_files[fix.filepath] = fix.new_content
                    else:
                        new_files[fix.filepath] = fix.new_content
                        self._workspace.write_file(path=fix.filepath, content=fix.new_content)
                new_hash = _hash("".join(sorted(f"{k}:{v}" for k, v in new_files.items())))
                return new_files, new_test_files, 0, new_hash

            # In TDD mode, override fix_target to always fix code (tests are the spec)
            is_tdd = getattr(self, '_strategy', None) == CodingStrategy.TDD
            if is_tdd and fix_target in ("tests", "both"):
                log("Orchestrator: TDD mode — overriding fix_target to 'code' (tests are the spec)")
                fix_target = "code"

            # Act on diagnosis fix_target
            if diagnosis and fix_target in ("tests", "both"):
                log("Orchestrator: diagnosis recommends fixing tests — regenerating...")
                enriched_prompt = (
                    f"{prompt}{self._get_scaffold_context()}\n\n"
                    f"DIAGNOSIS:\n{diagnosis_text}\n\n"
                    f"FIX PLAN:\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(fix_plan)) + "\n\n"
                    f"APPROACH: {suggested_approach}\n\n"
                    f"Previous failures:\n{failure_output}"
                )
                test_result = self._autotester.generate_multi(
                    problem_statement=enriched_prompt,
                    test_files=test_files,
                    source_code=current_files,
                )
                new_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}

                if fix_target == "both":
                    # Also repair code with diagnosis context
                    enriched_error = self._build_diagnosis_context(
                        diagnosis_text, diagnosis, fix_plan, suggested_approach,
                        failure_output,
                    )
                    all_files = {**current_files}
                    for fp, tests in new_test_files.items():
                        all_files[fp] = tests
                    repaired = self._autocoder.repair_multi(
                        current_files=all_files,
                        error_message=enriched_error,
                        architecture_context=architecture_context,
                    )
                    new_files = dict(current_files)
                    for ch in repaired.changes:
                        if ch.filepath in new_test_files:
                            new_test_files[ch.filepath] = ch.code
                        else:
                            new_files[ch.filepath] = ch.code
                            self._workspace.write_file(path=ch.filepath, content=ch.code)
                    return new_files, new_test_files, 0, None

                return current_files, new_test_files, 0, None

            if diagnosis and fix_target == "code":
                log("Orchestrator: diagnosis recommends fixing code — repairing with diagnosis...")
                enriched_error = self._build_diagnosis_context(
                    diagnosis_text, diagnosis, fix_plan, suggested_approach,
                    failure_output,
                )
                all_files = {**current_files}
                for fp, tests in current_test_files.items():
                    all_files[fp] = tests
                repaired = self._autocoder.repair_multi(
                    current_files=all_files,
                    error_message=enriched_error,
                    architecture_context=architecture_context,
                )
                new_files, new_test_files = self._apply_multi_repair(
                    repaired, current_files, current_test_files,
                )
                new_hash = _hash("".join(sorted(f"{k}:{v}" for k, v in new_files.items())))
                return new_files, new_test_files, 0, new_hash

            # No diagnosis available
            if is_tdd:
                # TDD mode: regenerate code from scratch (tests are the spec)
                log("Orchestrator: TDD stall — regenerating code from scratch...")
                code_result = self._autocoder.generate_multi(
                    issue_description=(
                        f"{prompt}\n\n"
                        f"YOUR CODE MUST PASS THESE TESTS:\n"
                        + "\n".join(f"── {fp} ──\n```{self._code_fence_lang}\n{t}\n```" for fp, t in current_test_files.items())
                        + f"\n\nPrevious failures:\n{failure_output}\n"
                        f"Generate a COMPLETELY FRESH implementation."
                    ),
                    target_files=target_files,
                    architecture_context=architecture_context,
                    existing_code={},
                )
                new_files = {ch.filepath: ch.code for ch in code_result.changes}
                return new_files, current_test_files, 0, None
            else:
                # Code-first mode: regenerate tests from spec (no source blob)
                log("Orchestrator: regenerating tests after stall...")
                test_result = self._autotester.generate_multi(
                    problem_statement=prompt + self._get_scaffold_context() + f"\n\nPrevious failures:\n{failure_output}",
                    test_files=test_files,
                    source_code=None,
                )
                new_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}
                return current_files, new_test_files, 0, None

        # Not stalled — one-shot minimal repair
        # Send only the single failing source file + its test file
        relevant_files = _extract_failing_pair(
            failure_output, current_files, current_test_files,
        )
        log(f"Orchestrator: one-shot repair with {len(relevant_files)} file(s)...")

        # Truncate error output to prevent massive prompts that cause JSON parse failures
        truncated_error = _truncate_error(failure_output)

        t0 = time.time()
        repaired = self._autocoder.repair_multi(
            current_files=relevant_files,
            error_message=truncated_error,
            architecture_context=architecture_context,
        )
        log(f"Orchestrator: repair done in {time.time() - t0:.1f}s")

        # Install any new dependencies declared in repair
        if repaired.dependencies:
            self._install_declared_dependencies(repaired.dependencies, log)

        new_files, new_test_files = self._apply_multi_repair(
            repaired, current_files, current_test_files,
        )
        new_hash = _hash("".join(sorted(f"{k}:{v}" for k, v in new_files.items())))
        return new_files, new_test_files, 0, new_hash

    def _apply_multi_repair(self, repaired, current_files, current_test_files):
        """Apply repair changes, separating code and test file updates."""
        new_files = dict(current_files)
        new_test_files = dict(current_test_files)
        for ch in repaired.changes:
            if ch.filepath in new_test_files:
                new_test_files[ch.filepath] = ch.code
            else:
                new_files[ch.filepath] = ch.code
                self._workspace.write_file(path=ch.filepath, content=ch.code)
        return new_files, new_test_files

    @staticmethod
    def _build_diagnosis_context(
        diagnosis_text, diagnosis, fix_plan, suggested_approach,
        failure_output: str,
    ) -> str:
        """Build an enriched error message from agentic diagnosis."""
        affected = getattr(diagnosis, "affected_files", []) if diagnosis else []
        category = getattr(diagnosis, "root_cause_category", "unknown") if diagnosis else "unknown"

        return (
            f"DIAGNOSIS (from comprehensive analysis):\n"
            f"Root cause: {diagnosis_text}\n"
            f"Category: {category}\n"
            f"Affected files: {', '.join(affected)}\n"
            f"Fix plan:\n" + "\n".join(f"  {i+1}. {step}" for i, step in enumerate(fix_plan)) + "\n"
            f"Approach: {suggested_approach}\n\n"
            f"ORIGINAL ERROR:\n{failure_output}"
        )

    # ── Regression detection ──────────────────────────────────────────────────

    def _get_passing_tests(self, log: Callable) -> set:
        """
        Run all existing tests in the workspace and return the set of
        test file paths that pass. Used as a baseline for regression detection.
        """
        try:
            test_paths = [
                str(f) for f in self._workspace.list_relative_files()
                if self._is_test_file(str(f))
            ]
            if not test_paths:
                return set()

            abs_paths = [str(self._workspace.path(tp)) for tp in test_paths]
            existing_paths = [p for p in abs_paths if Path(p).exists()]
            if not existing_paths:
                return set()

            passing = set()
            for test_path in existing_paths:
                call_spec = ExecutionCallSpec(symbol=self._test_symbol, args=[test_path])
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
            call_spec = ExecutionCallSpec(symbol=self._test_symbol, args=[abs_path])
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

    # ── Structural fixes ────────────────────────────────────────────────────

    def _fix_file_directory_collisions(self, current_files: dict, log: Callable) -> bool:
        """
        Detect and fix file/directory name collisions.

        If the autocoder generates e.g. 'pkg/models.py' but a directory
        'pkg/models/' with __init__.py already exists, move the file content
        into 'pkg/models/__init__.py' and remove the standalone .py file.

        Returns True if any collision was fixed.
        """
        try:
            workspace_root = self._workspace.root
            if not isinstance(workspace_root, Path):
                return False
        except Exception:
            return False

        fixed = False
        files_to_update = {}
        files_to_remove = []

        for filepath in list(current_files.keys()):
            p = Path(filepath)
            if p.suffix != ".py":
                continue

            # Check if a directory with the same stem exists
            dir_path = workspace_root / p.with_suffix("")
            if dir_path.is_dir():
                # Move the file content into the directory's __init__.py
                new_rel_path = str(p.with_suffix("")) + "/__init__.py"
                content = current_files[filepath]

                log(f"Orchestrator: collision detected — {filepath} vs {p.with_suffix('')}/")
                log(f"Orchestrator: moving {filepath} → {new_rel_path}")

                # Write to the __init__.py inside the directory
                self._workspace.write_file(path=new_rel_path, content=content)
                files_to_update[new_rel_path] = content
                files_to_remove.append(filepath)

                # Remove the standalone .py file from disk
                standalone = workspace_root / filepath
                if standalone.exists():
                    standalone.unlink()

                fixed = True

        # Update the current_files dict
        for new_path, content in files_to_update.items():
            current_files[new_path] = content
        for old_path in files_to_remove:
            current_files.pop(old_path, None)

        return fixed

    # ── Config file cleanup ────────────────────────────────────────────────────

    def _fix_bad_pytest_config(self, current_files: dict, error_detail: str, log: Callable) -> bool:
        """
        Detect and remove pytest config files that cause collection errors.

        The agentic debugger sometimes creates pytest.ini or conftest.py files
        with invalid options (e.g. --asyncio-mode=auto without pytest-asyncio).
        """
        if "unrecognized arguments" not in error_detail:
            return False

        try:
            workspace_root = self._workspace.root
            if not isinstance(workspace_root, Path):
                return False
        except Exception:
            return False

        fixed = False
        for config_file in ["pytest.ini", "setup.cfg", "tox.ini"]:
            config_path = workspace_root / config_file
            if config_path.exists():
                try:
                    content = config_path.read_text()
                    # Check if this config contains the problematic argument
                    # Extract the unrecognized arg from the error
                    import re
                    match = re.search(r"unrecognized arguments: (\S+)", error_detail)
                    if match and match.group(1) in content:
                        log(f"Orchestrator: removing bad config file {config_file} (contains {match.group(1)})")
                        config_path.unlink()
                        current_files.pop(config_file, None)
                        fixed = True
                except Exception:
                    pass

        return fixed

    # ── Model escalation ──────────────────────────────────────────────────────

    def _try_escalate_model(self, log: Callable, progression: Optional[ModelProgression] = None, agent: str = "all") -> bool:
        """
        Attempt to escalate to the next model in the given progression.

        Parameters
        ----------
        progression:
            The ModelProgression to escalate. Defaults to self._model_progression.
        agent:
            Which agent(s) to update: "autocoder", "autotester", "repair", or "all".
            When "all", updates all agents to the new model.

        Returns True if escalation happened, False if already at max or not configured.
        """
        prog = progression or self._model_progression
        if not prog or not self._client:
            return False
        if prog.is_at_max:
            return False

        new_model = prog.escalate()
        if new_model:
            if self._client_factory:
                fresh_client = self._client_factory(new_model)
                if agent == "all":
                    self._client = fresh_client
                    self._autocoder._client = fresh_client
                    self._autotester._client = fresh_client
                    if self._autodebugger:
                        self._autodebugger._client = fresh_client
                elif agent == "autocoder":
                    self._autocoder._client = fresh_client
                elif agent == "autotester":
                    self._autotester._client = fresh_client
                elif agent == "repair":
                    self._autocoder._client = fresh_client  # repair uses autocoder
                log(f"Orchestrator: escalated {agent} to model {new_model} (fresh client)")
            else:
                self._client.set_model(new_model)
                log(f"Orchestrator: escalated {agent} to model {new_model}")
            return True
        return False

    # ── Environment / package management ──────────────────────────────────────

    def _sync_environment_packages(self, log: Callable):
        """Load persisted packages from workspace DB and install into test env."""
        try:
            rows = self._workspace.db.get_packages()
            if rows:
                packages = [row["package"] for row in rows]
                env = self._find_installable_environment()
                if env:
                    env.install_packages(packages)
                    log(f"Orchestrator: synced {len(packages)} package(s) from workspace DB")
        except Exception:
            pass  # DB may not exist yet for fresh workspaces

    def _install_package(self, package: str, log: Callable):
        """Install a package into the test environment and persist to DB."""
        env = self._find_installable_environment()
        if env:
            env.install_packages([package])
            log(f"Orchestrator: installed package '{package}'")
        else:
            log(f"Orchestrator: WARNING — no installable environment found for '{package}'")

        # Update requirements.txt in workspace
        try:
            req_path = self._workspace.path("requirements.txt")
            existing = req_path.read_text() if req_path.exists() else ""
            existing_pkgs = {
                line.strip().split("==")[0].split(">=")[0].lower()
                for line in existing.splitlines()
                if line.strip() and not line.startswith("#")
            }
            if package.lower() not in existing_pkgs:
                with open(req_path, "a") as f:
                    f.write(f"{package}\n")
        except Exception:
            pass

        # Persist to workspace DB
        try:
            self._workspace.db.save_package(package)
        except Exception:
            pass

    def _find_installable_environment(self):
        """Find an environment that supports package installation (duck typing)."""
        if hasattr(self._test_environment, 'install_packages'):
            return self._test_environment
        if hasattr(self._autocoder, '_environment') and hasattr(self._autocoder._environment, 'install_packages'):
            return self._autocoder._environment
        return None

    def _install_declared_dependencies(self, dependencies: list, log: Callable):
        """Install dependencies declared by the LLM in its response."""
        if not dependencies:
            return

        # Check what's already installed
        already_installed = set()
        try:
            rows = self._workspace.db.get_packages()
            if rows:
                already_installed = {row["package"].lower() for row in rows}
        except Exception:
            pass

        to_install = [d for d in dependencies if d.lower() not in already_installed]
        if not to_install:
            return

        log(f"Orchestrator: installing {len(to_install)} LLM-declared dependency(ies): {', '.join(sorted(to_install))}")
        for pkg in to_install:
            self._install_package(pkg, log)

    def _get_installed_packages(self) -> list:
        """Read declared dependencies from requirements.txt if it exists."""
        try:
            req_path = self._workspace.path("requirements.txt")
            if req_path.exists():
                content = req_path.read_text()
                return [
                    line.strip() for line in content.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
        except Exception:
            pass
        return []

    def _build_workspace_manifest(self) -> str:
        """
        Build a lightweight manifest of existing workspace files.

        Extracts file paths, class names, function signatures, and exports
        so the LLM can understand the codebase without spending turns exploring.
        Language-agnostic: reads .py, .ts, .js, .cs files.
        """
        import ast

        manifest_lines = []
        try:
            rel_files = self._workspace.list_relative_files()
        except Exception:
            return ""

        for rel_path in sorted(str(p) for p in rel_files):
            if rel_path.startswith(".") or "__pycache__" in rel_path:
                continue
            if not any(rel_path.endswith(ext) for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".cs")):
                continue
            # Skip test files — those are separate
            if "test" in rel_path.lower() and rel_path.startswith("tests"):
                continue

            try:
                content = self._workspace.read_file(path=rel_path)
            except Exception:
                continue

            if not content or not content.strip():
                continue

            signatures = self._extract_signatures(rel_path, content)
            if signatures:
                manifest_lines.append(f"  {rel_path}: {', '.join(signatures)}")
            else:
                manifest_lines.append(f"  {rel_path}")

        if not manifest_lines:
            return ""
        return "\n".join(manifest_lines)

    def _extract_signatures(self, filepath: str, content: str) -> list:
        """Extract class/function signatures from a source file."""
        import ast
        signatures = []

        if not filepath.endswith(".py"):
            # For non-Python, use simple regex
            import re
            for match in re.finditer(r'(?:export\s+)?(?:class|interface|function|const|def)\s+(\w+)', content):
                signatures.append(match.group(1))
            return signatures[:10]

        try:
            tree = ast.parse(content, filename=filepath)
        except SyntaxError:
            return []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                methods = []
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        args = ", ".join(a.arg for a in item.args.args)
                        methods.append(f"{item.name}({args})")
                methods_str = f" [{', '.join(methods[:5])}]" if methods else ""
                signatures.append(f"class {node.name}{methods_str}")
            elif isinstance(node, ast.FunctionDef):
                args = ", ".join(a.arg for a in node.args.args)
                signatures.append(f"def {node.name}({args})")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        signatures.append(target.id)

        return signatures[:10]

    def _enrich_test_setup_hint(
        self,
        prompt: str,
        test_files: List[str],
        architecture_context: str,
        log: Callable,
    ) -> str:
        """
        Scan the workspace for app factories and routers, appending a few
        verified import lines to the prompt. Keeps it minimal — the
        architecture plan and engineer's hint are already in the prompt,
        this just adds ground-truth imports from files that exist on disk.
        """
        # Only enrich for integration/endpoint issues
        is_integration = any(
            kw in " ".join(test_files).lower()
            for kw in ["router", "endpoint", "api", "app", "factory", "integration"]
        )
        if not is_integration:
            prompt_lower = prompt.lower()
            is_integration = any(
                kw in prompt_lower
                for kw in ["endpoint", "route", "router", "api", "handler",
                            "controller", "middleware", "fastapi", "express",
                            "flask", "app factory", "testclient"]
            )
        if not is_integration:
            return prompt

        # Scan workspace for app factories and routers
        app_hints = []
        router_hints = []
        try:
            rel_files = self._workspace.list_relative_files()
            for rel_path in sorted(str(p) for p in rel_files):
                if rel_path.startswith(".") or "__pycache__" in rel_path:
                    continue
                if "test" in rel_path.lower():
                    continue
                if self._language == "python" and rel_path.endswith(".py"):
                    self._scan_python_for_hints(rel_path, app_hints, router_hints)
                elif self._language == "typescript" and rel_path.endswith((".ts", ".tsx")):
                    self._scan_typescript_for_hints(rel_path, app_hints, router_hints)
        except Exception:
            return prompt

        if not app_hints and not router_hints:
            return prompt

        # Build compact hint — just the import lines
        lines = []
        for h in app_hints + router_hints:
            lines.append(f"  {h}")
        verified = "\n".join(lines)

        if "TEST SETUP HINT:" in prompt:
            prompt += f"\n\nVERIFIED IMPORTS (from workspace):\n{verified}"
        else:
            prompt += f"\n\nTEST SETUP HINT (from workspace):\n{verified}"

        log(f"Orchestrator: enriched test hints ({len(app_hints)} app, {len(router_hints)} router from disk)")
        return prompt

    def _scan_python_for_hints(self, rel_path: str, app_hints: list, router_hints: list):
        """Scan a Python file for app factories and router definitions."""
        import ast

        try:
            content = self._workspace.read_file(path=rel_path)
        except Exception:
            return

        if not content or not content.strip():
            return

        try:
            tree = ast.parse(content, filename=rel_path)
        except SyntaxError:
            return

        module_path = rel_path.replace("/", ".").replace(".py", "")
        basename = rel_path.rsplit("/", 1)[-1].replace(".py", "")

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef):
                name_lower = node.name.lower()
                # App factory functions
                if any(kw in name_lower for kw in ["create_app", "make_app", "build_app", "app_factory"]):
                    app_hints.append(
                        f"from {module_path} import {node.name}; "
                        f"from fastapi.testclient import TestClient; "
                        f"client = TestClient({node.name}())"
                    )
                # Router builder functions
                elif "router" in name_lower or "build_router" in name_lower:
                    app_hints.append(
                        f"from {module_path} import {node.name}"
                    )

            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        name_lower = target.id.lower()
                        # Module-level app = FastAPI()
                        if name_lower == "app" and self._is_fastapi_call(node.value):
                            app_hints.append(
                                f"from {module_path} import app; "
                                f"from fastapi.testclient import TestClient; "
                                f"client = TestClient(app)"
                            )
                        # Module-level router = APIRouter()
                        elif "router" in name_lower:
                            router_hints.append(
                                f"from {module_path} import {target.id}"
                            )

    def _is_fastapi_call(self, node) -> bool:
        """Check if an AST node is a FastAPI() or similar call."""
        import ast
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ("FastAPI", "Flask", "Starlette"):
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("FastAPI", "Flask"):
                return True
        return False

    def _scan_typescript_for_hints(self, rel_path: str, app_hints: list, router_hints: list):
        """Scan a TypeScript file for app/router exports using regex."""
        import re

        try:
            content = self._workspace.read_file(path=rel_path)
        except Exception:
            return

        if not content or not content.strip():
            return

        module_path = rel_path.replace(".ts", "").replace(".tsx", "")

        # Look for exported app creation
        if re.search(r'export\s+(?:const|function)\s+(?:createApp|buildApp|app)', content):
            for m in re.finditer(r'export\s+(?:const|function)\s+(createApp|buildApp|app)\b', content):
                app_hints.append(f"import {{ {m.group(1)} }} from './{module_path}'")

        # Look for exported routers
        for m in re.finditer(r'export\s+(?:const|default)\s+(\w*[Rr]outer\w*)', content):
            router_hints.append(f"import {{ {m.group(1)} }} from './{module_path}'")

    def _get_passing_test_examples(self, max_examples: int = 2) -> str:
        """
        Get content of existing passing test files as examples for the autotester.

        Returns formatted string with test file contents, or empty string.
        """
        try:
            rel_files = self._workspace.list_relative_files()
        except Exception:
            return ""

        test_files = sorted(
            str(p) for p in rel_files
            if str(p).startswith("tests/") and str(p).endswith(".py")
            and "test_" in str(p) and "__pycache__" not in str(p)
        )

        if not test_files:
            return ""

        examples = []
        for tf in test_files[:max_examples]:
            try:
                content = self._workspace.read_file(path=tf)
                if content and content.strip() and len(content) < 3000:
                    examples.append(f"── {tf} ──\n{content}")
            except Exception:
                continue

        if not examples:
            return ""

        return (
            "\n\nEXISTING PASSING TESTS (follow these patterns):\n"
            + "\n\n".join(examples) + "\n"
        )

    def _get_scaffold_context(self) -> str:
        """Return formatted test scaffold context string, or empty string."""
        if not self._test_scaffold:
            return ""
        return (
            f"\n\nTEST SCAFFOLD (from the code author — use this as your starting point):\n"
            f"```{self._code_fence_lang}\n{self._test_scaffold}\n```\n"
        )

    def _run_preflight(
        self,
        current_files: dict,
        declared_dependencies: list,
        log: Callable,
    ) -> dict:
        """
        Run pre-flight validation on generated code.

        Checks import resolution and auto-stubs missing modules.
        Returns updated current_files dict with any stubs added.
        """
        validator = get_validator(self._language, self._workspace)
        if validator is None:
            return current_files

        result = validator.validate(current_files, declared_dependencies)

        # Write stubs to workspace
        for stub in result.stubs_created:
            self._workspace.write_file(path=stub.filepath, content=stub.content)
            current_files[stub.filepath] = stub.content

        if result.stubs_created or result.issues:
            log(result.summary())

        return current_files

    def _install_project_editable(self, log: Callable):
        """
        Run `pip install -e .` in the Docker container if a pyproject.toml or setup.py exists.
        This makes the project's own package importable (e.g. `from pet_groomer.api import app`).
        Skips silently after first failure to avoid wasting ~2s per iteration.
        """
        if self._editable_install_failed:
            return

        env = self._find_installable_environment()
        if env is None or not hasattr(env, '_container_id') or not hasattr(env, '_workspace_root'):
            return

        workspace = env._workspace_root
        has_pyproject = (workspace / "pyproject.toml").exists()
        has_setup = (workspace / "setup.py").exists()
        if not has_pyproject and not has_setup:
            return

        import subprocess as _sp

        # Ensure container is running
        env._ensure_container()

        log("Orchestrator: installing project in editable mode (pip install -e .)...")
        proc = _sp.run(
            ["docker", "exec", env._container_id,
             "pip", "install", "--no-cache-dir", "-e", "/workspace"],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            log("Orchestrator: project installed in editable mode.")
        else:
            self._editable_install_failed = True
            log(f"Orchestrator: pip install -e . failed (will skip future attempts): {proc.stderr[:200]}")

    def _proactive_package_install(
        self,
        current_files: dict,
        current_test_files: dict,
        log: Callable,
    ):
        """
        Scan all generated source and test files for imports, identify third-party
        packages, and install them before running tests.
        """
        all_files = {**current_files, **current_test_files}
        imports = _scan_imports(all_files)
        if not imports:
            return

        # Build set of workspace module names (to exclude from install)
        workspace_modules = set()
        for fp in list(current_files.keys()) + list(current_test_files.keys()):
            # Extract top-level module name from filepath
            parts = fp.replace("\\", "/").split("/")
            if parts:
                workspace_modules.add(parts[0].replace(".py", "").replace(".ts", "").replace(".tsx", ""))

        # Also add workspace files from disk
        try:
            for f in self._workspace.list_relative_files():
                parts = str(f).replace("\\", "/").split("/")
                if parts:
                    workspace_modules.add(parts[0].replace(".py", "").replace(".ts", "").replace(".tsx", ""))
        except Exception:
            pass

        third_party = _filter_third_party_packages(imports, workspace_modules, self._language)
        if not third_party:
            return

        # Check what's already installed (from workspace DB)
        already_installed = set()
        try:
            rows = self._workspace.db.get_packages()
            if rows:
                already_installed = {row["package"].lower() for row in rows}
        except Exception:
            pass

        to_install = [pkg for pkg in third_party if pkg.lower() not in already_installed]
        if not to_install:
            return

        log(f"Orchestrator: proactively installing {len(to_install)} detected package(s): {', '.join(sorted(to_install))}")
        for pkg in to_install:
            self._install_package(pkg, log)

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
        except AIInsufficientFunds:
            raise
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
    """Build failure message from test output. Only includes failing test files."""
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

    # Only include test files that are mentioned in the error output
    # (not ALL test files — that bloats the prompt unnecessarily)
    if test_files:
        error_text = "\n".join(parts)
        failing = {fp: code for fp, code in test_files.items() if fp in error_text}
        if failing:
            parts.append("\n\nFAILING TEST FILES:")
            for fp, tests in failing.items():
                parts.append(f"── {fp} ──\n{tests}")

    return "\n".join(parts) or "Tests failed with no additional output."


def _strip_source_ext(path: str) -> str:
    """Strip common source file extensions for fuzzy module matching."""
    for ext in (".test.tsx", ".test.ts", ".spec.tsx", ".spec.ts", ".tsx", ".ts", ".py"):
        if path.endswith(ext):
            return path[:-len(ext)]
    return path


def _order_by_dependencies(target_files: List[dict]) -> List[dict]:
    """
    Order target files so foundational files (models, types, config) come first.

    Uses simple heuristics based on common naming patterns rather than
    parsing imports, since the files haven't been generated yet.
    """
    def _priority(tf: dict) -> int:
        fp = tf["filepath"].lower()
        name = fp.rsplit("/", 1)[-1] if "/" in fp else fp
        # Foundational files first
        if "model" in name or "type" in name or "schema" in name:
            return 0
        if "config" in name or "setting" in name or "constant" in name:
            return 1
        if "repo" in name or "db" in name or "database" in name:
            return 2
        if "service" in name or "domain" in name:
            return 3
        if "route" in name or "endpoint" in name or "api" in name or "router" in name:
            return 4
        if "app" in name or "main" in name or "factory" in name:
            return 5
        return 3  # default: mid-priority

    return sorted(target_files, key=_priority)


def _find_source_for_test(test_filepath: str, source_files: dict) -> dict:
    """
    Given a test file path, find the source file(s) it likely tests.

    Returns a dict of {filepath: content} with just the relevant source file(s).
    Falls back to all source files if no match found (but this should be rare).
    """
    test_name = test_filepath.rsplit("/", 1)[-1] if "/" in test_filepath else test_filepath
    # Strip test prefix/suffix: test_models.py → models, models.test.ts → models
    base = _strip_source_ext(test_name)
    base = re.sub(r'^test_', '', base)
    base = re.sub(r'\.test$', '', base)
    base = re.sub(r'\.spec$', '', base)
    base = re.sub(r'_test$', '', base)

    matched = {}
    for fp, content in source_files.items():
        source_name = _strip_source_ext(fp.rsplit("/", 1)[-1] if "/" in fp else fp)
        if source_name == base or base in source_name or source_name in base:
            matched[fp] = content

    # If no match, send all (fallback for unusual naming)
    return matched if matched else dict(source_files)


def _find_tests_for_source(source_filepath: str, test_files: dict) -> dict:
    """
    Given a source file path, find the test file(s) that test it.

    Returns a dict of {filepath: test_content}.
    Falls back to all test files if no match found.
    """
    source_name = source_filepath.rsplit("/", 1)[-1] if "/" in source_filepath else source_filepath
    base = _strip_source_ext(source_name)

    matched = {}
    for fp, content in test_files.items():
        test_name = _strip_source_ext(fp.rsplit("/", 1)[-1] if "/" in fp else fp)
        # test_models → models, models.test → models
        clean = re.sub(r'^test_', '', test_name)
        clean = re.sub(r'\.test$', '', clean)
        clean = re.sub(r'\.spec$', '', clean)
        clean = re.sub(r'_test$', '', clean)
        if clean == base or base in clean or clean in base:
            matched[fp] = content

    return matched if matched else dict(test_files)


def _extract_failing_pair(
    failure_output: str,
    current_files: dict,
    current_test_files: dict,
) -> dict:
    """
    Extract the single failing test file + the source file it tests.

    Keeps the repair prompt minimal (one source + one test) so it fits
    within tight TPM limits. Falls back to _extract_relevant_files if
    we can't identify a single pair.
    """
    # Find the first test file mentioned in the error output
    failing_test_fp = None
    for test_fp in current_test_files:
        if test_fp in failure_output:
            failing_test_fp = test_fp
            break
        module_path = _strip_source_ext(test_fp.replace("/", "."))
        if module_path in failure_output:
            failing_test_fp = test_fp
            break

    if not failing_test_fp:
        # Can't identify the failing test — fall back to relevant files
        return _extract_relevant_files(failure_output, current_files, current_test_files)

    # Find the source file that this test imports
    result = {failing_test_fp: current_test_files[failing_test_fp]}
    test_content = current_test_files[failing_test_fp]
    for code_fp in current_files:
        base_name = _strip_source_ext(code_fp.replace("/", ".")).split(".")[-1]
        if base_name in test_content or code_fp in failure_output:
            result[code_fp] = current_files[code_fp]
            break  # Only one source file

    # If no source file matched, include the first source file mentioned in the error
    if len(result) == 1:
        for code_fp in current_files:
            if code_fp in failure_output:
                result[code_fp] = current_files[code_fp]
                break

    return result


def _extract_relevant_files(
    failure_output: str,
    current_files: dict,
    current_test_files: dict,
) -> dict:
    """
    Extract only the files mentioned in the failure output (plus their imports).
    Returns a dict of filepath → content for the repair prompt.
    Falls back to all files if we can't determine relevance.
    """
    mentioned = set()

    # Find file paths mentioned in the error output
    all_paths = set(current_files.keys()) | set(current_test_files.keys())
    for path in all_paths:
        # Check if the file path or module name appears in the error
        if path in failure_output:
            mentioned.add(path)
        # Also check module-style references (e.g., "pet_groomer.models" for "pet_groomer/models.py")
        module_path = _strip_source_ext(path.replace("/", "."))
        if module_path in failure_output:
            mentioned.add(path)

    # If no files were mentioned, send all files (fallback)
    if not mentioned:
        all_files = {**current_files}
        for fp, tests in current_test_files.items():
            all_files[fp] = tests
        return all_files

    # For each mentioned test file, also include the code files it likely imports
    for test_fp in list(mentioned):
        if test_fp in current_test_files:
            test_content = current_test_files[test_fp]
            # Find imports from the test file that reference our code files
            for code_fp in current_files:
                module_name = _strip_source_ext(code_fp.replace("/", "."))
                # Check if the test file imports from this module
                base_name = module_name.split(".")[-1]
                if base_name in test_content:
                    mentioned.add(code_fp)

    # For each mentioned code file, include the test files that test it
    for code_fp in list(mentioned):
        if code_fp in current_files:
            base_name = _strip_source_ext(code_fp.replace("/", ".")).split(".")[-1]
            for test_fp, test_content in current_test_files.items():
                if base_name in test_content:
                    mentioned.add(test_fp)

    # Build result dict
    relevant = {}
    for fp in mentioned:
        if fp in current_files:
            relevant[fp] = current_files[fp]
        elif fp in current_test_files:
            relevant[fp] = current_test_files[fp]

    return relevant


def _scan_imports(files: dict) -> Set[str]:
    """
    Scan Python/TypeScript source code for import statements and return
    the set of top-level package names that are third-party (not stdlib, not local).
    """
    packages = set()
    for filepath, content in files.items():
        if filepath.endswith(".py"):
            # Python: import foo / from foo import bar
            for match in re.finditer(r'^(?:from|import)\s+(\w+)', content, re.MULTILINE):
                packages.add(match.group(1))
        elif filepath.endswith((".ts", ".tsx", ".js", ".jsx")):
            # TypeScript/JS: import ... from 'package' / require('package')
            for match in re.finditer(r'''(?:from\s+['"]|require\s*\(\s*['"])([^./'"]\S*?)['"/]''', content):
                pkg = match.group(1)
                # Strip @scope/name to just @scope/name
                if pkg.startswith("@"):
                    # @scope/name → keep as-is
                    packages.add(pkg)
                else:
                    packages.add(pkg)
    return packages


# Standard library module names (Python 3.8+)
_STDLIB_MODULES = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else {
    'abc', 'aifc', 'argparse', 'array', 'ast', 'asynchat', 'asyncio', 'asyncore',
    'atexit', 'audioop', 'base64', 'bdb', 'binascii', 'binhex', 'bisect',
    'builtins', 'bz2', 'calendar', 'cgi', 'cgitb', 'chunk', 'cmath', 'cmd',
    'code', 'codecs', 'codeop', 'collections', 'colorsys', 'compileall',
    'concurrent', 'configparser', 'contextlib', 'contextvars', 'copy', 'copyreg',
    'cProfile', 'crypt', 'csv', 'ctypes', 'curses', 'dataclasses', 'datetime',
    'dbm', 'decimal', 'difflib', 'dis', 'distutils', 'doctest', 'email',
    'encodings', 'enum', 'errno', 'faulthandler', 'fcntl', 'filecmp', 'fileinput',
    'fnmatch', 'formatter', 'fractions', 'ftplib', 'functools', 'gc', 'getopt',
    'getpass', 'gettext', 'glob', 'grp', 'gzip', 'hashlib', 'heapq', 'hmac',
    'html', 'http', 'idlelib', 'imaplib', 'imghdr', 'imp', 'importlib', 'inspect',
    'io', 'ipaddress', 'itertools', 'json', 'keyword', 'lib2to3', 'linecache',
    'locale', 'logging', 'lzma', 'mailbox', 'mailcap', 'marshal', 'math',
    'mimetypes', 'mmap', 'modulefinder', 'multiprocessing', 'netrc', 'nis',
    'nntplib', 'numbers', 'operator', 'optparse', 'os', 'ossaudiodev',
    'pathlib', 'pdb', 'pickle', 'pickletools', 'pipes', 'pkgutil', 'platform',
    'plistlib', 'poplib', 'posix', 'posixpath', 'pprint', 'profile', 'pstats',
    'pty', 'pwd', 'py_compile', 'pyclbr', 'pydoc', 'queue', 'quopri', 'random',
    're', 'readline', 'reprlib', 'resource', 'rlcompleter', 'runpy', 'sched',
    'secrets', 'select', 'selectors', 'shelve', 'shlex', 'shutil', 'signal',
    'site', 'smtpd', 'smtplib', 'sndhdr', 'socket', 'socketserver', 'sqlite3',
    'sre_compile', 'sre_constants', 'sre_parse', 'ssl', 'stat', 'statistics',
    'string', 'stringprep', 'struct', 'subprocess', 'sunau', 'symtable', 'sys',
    'sysconfig', 'syslog', 'tabnanny', 'tarfile', 'telnetlib', 'tempfile',
    'termios', 'test', 'textwrap', 'threading', 'time', 'timeit', 'tkinter',
    'token', 'tokenize', 'trace', 'traceback', 'tracemalloc', 'tty', 'turtle',
    'turtledemo', 'types', 'typing', 'unicodedata', 'unittest', 'urllib', 'uu',
    'uuid', 'venv', 'warnings', 'wave', 'weakref', 'webbrowser', 'winreg',
    'winsound', 'wsgiref', 'xdrlib', 'xml', 'xmlrpc', 'zipapp', 'zipfile',
    'zipimport', 'zlib', '_thread',
}

# Common test framework names that are part of the stdlib or always available
_TEST_BUILTINS = {'pytest', 'unittest', 'doctest', 'mock'}


def _filter_third_party_packages(
    imports: Set[str],
    workspace_modules: Set[str],
    language: str = "python",
) -> Set[str]:
    """
    Filter imports to only third-party packages (not stdlib, not workspace modules).
    """
    if language == "python":
        return {
            pkg for pkg in imports
            if pkg not in _STDLIB_MODULES
            and pkg not in _TEST_BUILTINS
            and pkg not in workspace_modules
            and not pkg.startswith("_")
        }
    else:
        # TypeScript: filter out node builtins
        node_builtins = {
            'fs', 'path', 'os', 'http', 'https', 'url', 'util', 'stream',
            'events', 'buffer', 'crypto', 'child_process', 'cluster', 'net',
            'dns', 'tls', 'assert', 'zlib', 'readline', 'querystring',
        }
        return {
            pkg for pkg in imports
            if pkg not in node_builtins
            and pkg not in workspace_modules
        }


def _truncate_error(error_output: str, max_chars: int = 4000) -> str:
    """Truncate error output to prevent massive prompts that blow TPM limits."""
    if len(error_output) <= max_chars:
        return error_output
    # Keep the first part (error summary) and last part (most relevant failures)
    half = max_chars // 2
    return (
        error_output[:half]
        + f"\n\n... [truncated {len(error_output) - max_chars} chars] ...\n\n"
        + error_output[-half:]
    )


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
