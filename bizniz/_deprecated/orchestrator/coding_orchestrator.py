"""
CodingOrchestrator

Iteratively generates code (via Coder) and tests (via Tester), runs the
tests, and repairs the code on failure until the tests pass or safeguards trigger.

Strategies
----------
- TDD (default): generate tests first from the spec, then code to pass them.
  Debug loop fixes code only — tests are the source of truth.
- CODE_FIRST: generate code first, then tests. Debug loop can fix either.
  Used as a fallback when TDD fails.

Iteration flow (TDD)
---------------------
1.  Tester.generate_multi(prompt) → contract tests (no source code)
2.  Coder.generate_multi(prompt + test_code) → generate code to pass tests
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

from bizniz.agents.coder.coder import Coder
from bizniz.agents.debugger.quick import QuickDebugger
from bizniz.agents.debugger.agentic import AgenticDebugger
from bizniz.tester.tester import Tester
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import ExecutionCallSpec
from bizniz.orchestrator.model_progression import ModelProgression
from bizniz.orchestrator.stall_detector import StallDetector
from bizniz.workspace.base_workspace import BaseWorkspace

from bizniz.clients.errors import AIInsufficientFunds
from bizniz.agents.coder.types import FileChange
from bizniz.tester.types import GeneratedTestFile
from bizniz.orchestrator.strategy import CodingStrategy
from bizniz.preflight.registry import get_validator
from bizniz._deprecated.languages import get_language_strategy, LanguageStrategy
from bizniz.orchestrator.types import (
    OrchestratorResult,
    OrchestratorStalledError,
    OrchestratorMaxIterationsError,
)


# Project config files the repair loop is always allowed to edit, even when
# they are NOT in the current issue's target_files. These are universal
# project config (build, test runner, dependency manifests, container) that
# the AI legitimately needs to fix when a misconfiguration is the actual root
# cause of test failures (e.g. jest preset missing, requirements.txt incomplete).
# Match by basename so paths anywhere in the workspace qualify.
_CONFIG_FILENAMES = frozenset({
    # Python project config
    "pyproject.toml", "setup.cfg", "setup.py",
    "pytest.ini", "tox.ini", "conftest.py",
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
    # JS/TS project config
    "package.json", "tsconfig.json",
    "tsconfig.app.json", "tsconfig.spec.json", "tsconfig.base.json",
    # Test runner configs
    "jest.config.js", "jest.config.ts", "jest.config.mjs",
    "jest.config.cjs", "jest.config.json",
    "vitest.config.js", "vitest.config.ts",
    "karma.conf.js",
    # Build configs
    "vite.config.js", "vite.config.ts",
    "webpack.config.js",
    "angular.json",
    "babel.config.js", ".babelrc.json",
    # Containerization
    "Dockerfile",
    "docker-compose.yml", "docker-compose.yaml",
})


def _is_config_file(filepath: str) -> bool:
    """True when the path's basename is in the universal config allowlist."""
    if not filepath:
        return False
    basename = Path(filepath).name
    return basename in _CONFIG_FILENAMES


def _retag_client_for_agent(client, agent) -> None:
    """Set ``client._caller_agent`` to the agent's class name (lowercased).

    BaseAIAgent.__init__ tags the original client at construction time
    so cost-tracker records show coder/tester/quickdebugger correctly.
    On model escalation, the orchestrator hands each agent a fresh
    client from the factory — that fresh client wasn't constructed via
    BaseAIAgent and has no tag, so its calls would otherwise show up
    as ``agent=unknown`` in the cost report. This helper restores the
    tag after a swap.
    """
    try:
        client._caller_agent = type(agent).__name__.lower()
    except Exception:
        pass


class CodingOrchestrator:
    """
    Orchestrates Coder + Tester + QuickDebugger in an iterative repair loop.

    Parameters
    ----------
    coder:
        A configured Coder instance.
    tester:
        A configured Tester instance.
    quick_debugger:
        Optional QuickDebugger instance for intelligent failure diagnosis.
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
        coder: Coder,
        tester: Tester,
        test_environment: BaseExecutionEnvironment,
        workspace: BaseWorkspace,
        quick_debugger: Optional[QuickDebugger] = None,
        client: Optional[BaseAIClient] = None,
        client_factory: Optional[Callable[[str], BaseAIClient]] = None,
        debugger_factory: Optional[Callable[[], AgenticDebugger]] = None,
        model_progression: Optional[ModelProgression] = None,
        coder_progression: Optional[ModelProgression] = None,
        tester_progression: Optional[ModelProgression] = None,
        repair_progression: Optional[ModelProgression] = None,
        stall_threshold: int = 2,
        agentic_debug_threshold: int = 2,
        max_iterations: int = 20,
        on_status_message: Optional[Callable[[str], None]] = None,
        language: str = "python",
        enable_agentic_debug: bool = True,
        stall_recovery: str = "full",
    ):
        self._coder = coder
        self._tester = tester
        self._quick_debugger = quick_debugger
        self._test_environment = test_environment
        self._workspace = workspace
        self._client = client
        self._client_factory = client_factory
        self._debugger_factory = debugger_factory
        # Per-agent progressions (fall back to shared model_progression)
        self._model_progression = model_progression
        self._autocoder_progression = coder_progression or model_progression
        self._autotester_progression = tester_progression or model_progression
        self._repair_progression = repair_progression or model_progression
        self._stall_threshold = stall_threshold
        self._agentic_debug_threshold = agentic_debug_threshold
        self._max_iterations = max_iterations
        self._on_status_message = on_status_message
        self._language = language
        self._lang: LanguageStrategy = get_language_strategy(language)
        self._enable_agentic_debug = enable_agentic_debug
        self._stall_recovery = stall_recovery  # "full", "regenerate", or "none"
        self._stall_detector = StallDetector(
            consecutive_fail_threshold=stall_threshold,
        )
        self._stall_cycle_count = 0
        self._editable_install_failed = False  # skip pip install -e . after first failure
        self._test_scaffold = ""  # cached scaffold from coder for test regeneration
        self._readonly_filter_warning = ""  # set when read-only changes are filtered from repair
        self._preflight_issues = []  # unresolved import issues with "did you mean?" hints

        # Override system prompts for non-Python languages
        if language != "python":
            self._apply_language_system_prompts()

        # Append skeleton directory conventions if the workspace ships
        # a SKELETON.md. Done after language overrides so it layers on
        # top of either the default or the language-specific prompt.
        self._apply_skeleton_conventions()

    def _apply_language_system_prompts(self):
        """Override coder/tester system prompts for the configured language."""
        eval_env = ""
        if hasattr(self._test_environment, 'describe'):
            eval_env = self._test_environment.describe()
        coder_prompt = self._lang.get_coder_system_prompt(eval_env)
        self._coder.set_system_prompt_override(coder_prompt)

        tester_prompt = self._lang.get_tester_system_prompt()
        self._tester.set_system_prompt_override(tester_prompt)

    def _apply_skeleton_conventions(self):
        from bizniz.workspace.skeleton_conventions import load_skeleton_conventions
        section = load_skeleton_conventions(self._workspace)
        if not section:
            return
        for agent in (self._coder, self._tester, self._quick_debugger):
            base = agent._system_prompt_override or agent._process_system_prompt
            agent.set_system_prompt_override(base + "\n\n" + section)

    # ── Language helpers (delegated to LanguageStrategy) ─────────────────────

    @property
    def _test_symbol(self) -> str:
        return self._lang.test_symbol

    @property
    def _code_fence_lang(self) -> str:
        return self._lang.code_fence_lang

    @property
    def _language_prefix(self) -> str:
        return self._lang.language_prefix

    def _is_test_file(self, filepath: str) -> bool:
        return self._lang.is_test_file(filepath)

    def _strip_extension(self, filepath: str) -> str:
        return self._lang.strip_extension(filepath)

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
            test_result = self._tester.process_from_prompt(
                prompt=prompt,
                output_path=test_filename,
                code_filename=code_filename,
            )
            current_tests = _extract_tests(test_result.test_files, test_filename)

            log("Orchestrator [TDD]: generating code to pass tests...")
            test_context = f"\n\nYOUR CODE MUST PASS THESE TESTS:\n```python\n{current_tests}\n```"
            code_result = self._coder.generate_only(
                prompt=prompt + test_context,
                filename=code_filename,
            )
            current_code = _extract_code(code_result.changes, code_filename) or self._workspace.read_file(code_filename)
        else:
            # Code-first: code then tests
            log("Orchestrator [CODE_FIRST]: generating initial code...")
            code_result = self._coder.generate_only(
                prompt=prompt,
                filename=code_filename,
            )
            current_code = _extract_code(code_result.changes, code_filename) or self._workspace.read_file(code_filename)

            log("Orchestrator [CODE_FIRST]: generating contract tests...")
            test_result = self._tester.process_from_prompt(
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
                error_detail = ""
                if eval_result.error and eval_result.error.traceback:
                    error_detail = eval_result.error.traceback
                elif eval_result.stdout:
                    error_detail = eval_result.stdout
                if not error_detail and eval_result.stderr:
                    error_detail = eval_result.stderr
                elif eval_result.stderr and eval_result.stderr not in error_detail:
                    error_detail += f"\nstderr: {eval_result.stderr}"

                # Config file parse error — repair the source, not the tests
                is_config_error = (
                    "exited with code 4" in (eval_result.error.message or "")
                    and any(
                        cf in error_detail
                        for cf in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini")
                    )
                )
                if is_config_error:
                    log("Orchestrator: config file parse error — repairing source...")
                    try:
                        repair_result = self._coder.repair(
                            code=current_code,
                            error_message=(
                                f"A config file has a syntax/parse error that prevents pytest "
                                f"from starting. Fix the broken file.\n\n"
                                f"FULL ERROR:\n{error_detail}"
                            ),
                            code_filename=code_filename,
                        )
                        current_code = repair_result.code
                        self._workspace.write_file(path=code_filename, content=current_code)
                        self._install_project_editable(log)
                        stale_count = 0
                        previous_code_hash = None
                        continue
                    except AIInsufficientFunds:
                        raise
                    except Exception as e:
                        log(f"Orchestrator: config repair failed ({type(e).__name__}: {e})")

                log("Orchestrator: test collection error — regenerating tests...")
                regen_prompt = (
                    f"{prompt}\n\n"
                    f"Here is the current implementation that tests must be written for:\n"
                    f"```python\n{current_code}\n```\n\n"
                    f"IMPORTANT: The previous test file had errors and could not be collected by pytest.\n"
                    f"The error was:\n{error_detail}\n\n"
                    f"Make sure all test functions use only defined fixtures or pytest.mark.parametrize.\n"
                    f"Do NOT use undefined fixture parameters in test function signatures."
                )
                test_result = self._tester.process_from_prompt(
                    prompt=regen_prompt,
                    output_path=test_filename,
                    code_filename=code_filename,
                )
                current_tests = _extract_tests(test_result.test_files, test_filename)
                stale_count = 0
                previous_code_hash = None
                continue

            # ── QuickDebugger-driven diagnosis ─────────────────────────────────
            try:
                if self._quick_debugger is not None:
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

                # ── Heuristic fallback (no quick_debugger) ──────────────────────
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
            except (AIInsufficientFunds, OrchestratorMaxIterationsError):
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
        dependency_edges: Optional[list] = None,
        prior_test_files: Optional[Set[str]] = None,
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
        dependency_edges:
            Optional list of DependencyEdge objects from the ArchitecturePlan.
            Used for exact graph lookups in the repair loop.
        prior_test_files:
            Optional set of test file paths from previously completed issues.
            Only these files are checked for regressions. If None, all passing
            tests in the workspace are used (which may include stubs from
            future issues).
        """
        self._dependency_edges = dependency_edges or []

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        self._strategy = strategy
        log(f"Orchestrator: using {strategy.value} strategy")

        # Scope architecture context to this issue's files + their dependencies.
        # Prevents the LLM from seeing unbuilt modules (e.g. an app factory
        # that's a future issue) and trying to import from them.
        architecture_context = self._scope_architecture_context(
            architecture_context, target_files,
        )

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
                self._coder._client = fresh_client
                self._tester._client = fresh_client
                if self._quick_debugger:
                    self._quick_debugger._client = fresh_client
                # Re-tag the fresh clients so cost-tracker records the
                # right agent. BaseAIAgent.__init__ tags the original
                # client at construction time, but the factory hands
                # back a brand-new client per escalation that hasn't
                # been through that path.
                _retag_client_for_agent(fresh_client, self._coder)
                _retag_client_for_agent(fresh_client, self._tester)
                if self._quick_debugger:
                    _retag_client_for_agent(fresh_client, self._quick_debugger)
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
        _container_rebuilt = False
        _wall_clock_start = time.time()

        # ── Load existing code for files being modified ──────────────────────
        existing_code = {}
        for tf in target_files:
            if tf.get("action") == "modify" and self._workspace.exists(path=tf["filepath"]):
                existing_code[tf["filepath"]] = self._workspace.read_file(path=tf["filepath"])

        # ── Snapshot passing tests before we start (regression baseline) ─────
        baseline_passing = self._get_passing_tests(log, restrict_to=prior_test_files)

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

        # ── Import map: exact import statements for every workspace module ────
        import_map = self._build_import_map()
        if import_map:
            extra_context += (
                f"\n\nIMPORT MAP (use these EXACT import paths — do NOT guess):\n{import_map}\n"
                "\nIMPORTANT: Always use absolute imports as shown above. "
                "NEVER use relative imports (from . or from ..) — they will be rejected.\n"
            )

        # ── Installed packages: tell the LLM what's available ────────────────
        installed_pkgs = self._get_installed_packages()
        if installed_pkgs:
            extra_context += (
                f"\n\nINSTALLED PACKAGES (available in the environment):\n{installed_pkgs}\n"
                "\nOnly import third-party packages from this list. "
                "If you need a package not listed here, declare it in your response's "
                "\"dependencies\" array.\n"
            )

        # ── Stub file warnings ─────────────────────────────────────────────────
        # Tell the LLM which files are still stubs so it doesn't import from them
        target_fps = {tf["filepath"] for tf in target_files}
        stub_warnings = []
        try:
            for rel_path in self._workspace.list_relative_files():
                rel_str = str(rel_path)
                if rel_str in target_fps or not rel_str.endswith(".py"):
                    continue
                if rel_str.startswith("tests") or self._should_skip_path(rel_str):
                    continue
                try:
                    content = self._workspace.read_file(path=rel_str)
                    if content and self._is_stub_file(rel_str, content):
                        stub_warnings.append(rel_str)
                except Exception:
                    pass
        except Exception:
            pass
        # Transitive stubs: files that import from stub files are also unsafe
        if stub_warnings:
            import ast as _ast
            stub_modules = set()
            for sw in stub_warnings:
                # Convert filepath to module name (e.g. "pkg/foo.py" -> "pkg.foo")
                mod = sw.replace("/", ".").replace("\\", ".")
                if mod.endswith(".py"):
                    mod = mod[:-3]
                stub_modules.add(mod)

            try:
                for rel_path in self._workspace.list_relative_files():
                    rel_str = str(rel_path)
                    if rel_str in target_fps or not rel_str.endswith(".py"):
                        continue
                    if rel_str in stub_warnings:
                        continue
                    if rel_str.startswith("tests") or self._should_skip_path(rel_str):
                        continue
                    try:
                        content = self._workspace.read_file(path=rel_str)
                        if not content:
                            continue
                        tree = _ast.parse(content, filename=rel_str)
                        for node in _ast.iter_child_nodes(tree):
                            if isinstance(node, _ast.Import):
                                for alias in node.names:
                                    if any(alias.name.startswith(sm) for sm in stub_modules):
                                        stub_warnings.append(rel_str)
                                        raise StopIteration
                            elif isinstance(node, _ast.ImportFrom) and node.module:
                                if any(node.module.startswith(sm) for sm in stub_modules):
                                    stub_warnings.append(rel_str)
                                    raise StopIteration
                    except StopIteration:
                        pass
                    except Exception:
                        pass
            except Exception:
                pass

        if stub_warnings:
            extra_context += (
                "\n\nSTUB FILES — NOT YET IMPLEMENTED (do NOT import from these):\n"
                + "\n".join(f"  - {fp}" for fp in stub_warnings)
                + "\nThese files contain stubs or import from stubs. "
                "Importing from them will cause errors.\n"
            )

        # Tell the LLM to test source files directly, not through app
        # factories or entry points that may not exist yet.
        extra_context += (
            "\n\nTEST ISOLATION: In tests, import ONLY from the source files "
            "listed as targets and their declared dependencies. Do NOT import "
            "from an app factory, main entry point, or any module not listed "
            "in your target files. Create any needed test fixtures (e.g. a "
            "FastAPI TestClient) directly in the test file.\n"
        )

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

        # ── Container import validation ────────────────────────────────────
        # Batch-try all imports in Docker, auto-fix bad paths, pip-install
        # missing deps, and ask the agent about ambiguous cases.
        current_files = self._validate_imports_in_container(
            current_files, current_test_files, architecture_context, log,
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

                    # Widen writable scope for regression repair: include the
                    # regressing test files and any source files they import so
                    # the repair loop can fix both sides of the mismatch.
                    regression_test_files = dict(current_test_files)
                    regression_target_files = list(target_files)
                    _existing_targets = {tf["filepath"] for tf in regression_target_files}
                    for reg_path in regressions:
                        if reg_path not in regression_test_files:
                            try:
                                regression_test_files[reg_path] = self._workspace.read_file(path=reg_path)
                            except Exception:
                                pass
                        # Also make source files imported by regressing tests writable
                        try:
                            reg_content = self._workspace.read_file(path=reg_path)
                            for imp_match in re.finditer(
                                r'^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))',
                                reg_content, re.MULTILINE,
                            ):
                                module_path = (imp_match.group(1) or imp_match.group(2) or "")
                                parts = module_path.split(".")
                                for k in range(len(parts), 0, -1):
                                    candidate = "/".join(parts[:k]) + ".py"
                                    if candidate not in _existing_targets:
                                        try:
                                            if self._workspace.path(candidate).exists():
                                                regression_target_files.append(
                                                    {"filepath": candidate, "action": "modify"}
                                                )
                                                _existing_targets.add(candidate)
                                                break
                                        except Exception:
                                            pass
                        except Exception:
                            pass

                    try:
                        current_files, current_test_files, stale_count, previous_code_hash = (
                            self._handle_multi_failure(
                                prompt=prompt,
                                failure_output=failure_output,
                                current_files=current_files,
                                current_test_files=regression_test_files,
                                target_files=regression_target_files,
                                test_files=list(regression_test_files.keys()),
                                architecture_context=architecture_context,
                                stale_count=stale_count,
                                previous_code_hash=previous_code_hash,
                                log=log,
                            )
                        )
                    except (AIInsufficientFunds, OrchestratorMaxIterationsError):
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

                log(f"Orchestrator: collection error detail: {error_detail[:2000]}")

                # Container rebuild: if we've had 3+ collection errors and
                # a declared package can't be imported, the container state
                # is stale. Rebuild from scratch.
                if collection_error_count >= 3 and not _container_rebuilt:
                    env = self._find_installable_environment()
                    if env and hasattr(env, 'stop') and hasattr(env, '_ensure_container'):
                        # Check if a requirements.txt package is failing
                        try:
                            pkgs = self._workspace.db.get_packages()
                            declared = {r["package"].lower() for r in pkgs} if pkgs else set()
                        except Exception:
                            declared = set()
                        pkg_missing = any(
                            pkg in error_detail.lower() for pkg in declared
                        )
                        if pkg_missing:
                            log("Orchestrator: container state stale — rebuilding...")
                            try:
                                env.stop()
                                env._ensure_container()
                                if declared:
                                    env.install_packages(sorted(declared))
                                collection_error_count = 0
                                _container_rebuilt = True
                                continue
                            except Exception as exc:
                                log(f"Orchestrator: container rebuild failed ({exc})")

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

                # Config file parse error (e.g. malformed pyproject.toml, setup.cfg)
                # — pytest can't even start. Repair the config file, not the tests.
                is_config_error = (
                    "exited with code 4" in (eval_result.error.message or "")
                    and any(
                        cf in error_detail
                        for cf in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini")
                    )
                )
                if is_config_error and collection_error_count <= 3:
                    log("Orchestrator: config file parse error — repairing source...")
                    all_files = {**current_files}
                    for fp, tests in current_test_files.items():
                        all_files[fp] = tests
                    try:
                        repaired = self._coder.repair_multi(
                            current_files=all_files,
                            error_message=(
                                f"A config file has a syntax/parse error that prevents pytest "
                                f"from starting. Fix the broken file.\n\n"
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
                        self._install_project_editable(log)
                        stale_count = 0
                        previous_code_hash = None
                        continue
                    except AIInsufficientFunds:
                        raise
                    except Exception as e:
                        log(f"Orchestrator: config repair failed ({type(e).__name__}: {e})")

                # If collection error involves our source files, repair the source
                # code instead of just regenerating tests. Detects: ImportError,
                # ModuleNotFoundError, or any traceback that chains through our
                # source files.
                source_import_error = self._is_source_import_error(
                    error_detail, current_files,
                )
                if source_import_error and collection_error_count <= 3:
                    log("Orchestrator: collection error caused by source code import — repairing code...")

                    # First, try auto-fixing imports directly from the workspace
                    auto_fixed = self._auto_fix_source_imports(current_files, error_detail, log)
                    if auto_fixed:
                        stale_count = 0
                        previous_code_hash = None
                        continue

                    # Fall back to LLM repair
                    all_files = {**current_files}
                    for fp, tests in current_test_files.items():
                        all_files[fp] = tests
                    try:
                        repaired = self._coder.repair_multi(
                            current_files=all_files,
                            error_message=(
                                f"Test collection failed because our SOURCE CODE has broken imports. "
                                f"The test imports our module, and our module tries to import "
                                f"from a path that does not exist.\n\n"
                                f"IMPORTANT: Use discovery tools to check what modules actually "
                                f"exist in the workspace. The import paths in our source code may "
                                f"be wrong — for example, importing from pet_groomer.api.models "
                                f"when the actual module is at pet_groomer.models.\n\n"
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
                            self._coder.clear_message_history()
                            self._tester.clear_message_history()

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
                            code_result = self._coder.generate_multi(
                                issue_description=enriched_code_prompt,
                                target_files=target_files,
                                architecture_context=architecture_context,
                                existing_code=existing_code,
                            )
                            current_files = {ch.filepath: ch.code for ch in code_result.changes}
                            test_result = self._tester.generate_multi(
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

                # Clear tester history to prevent token bloat
                self._tester.clear_message_history()

                log("Orchestrator: test collection error — regenerating tests...")

                # Extract failing imports so we can explicitly warn the LLM
                import re as _re
                _bad_imports = _re.findall(
                    r'>\s*((?:from\s+\S+\s+import\s+\S+|import\s+\S+))',
                    error_detail,
                )
                _bad_warning = ""
                if _bad_imports:
                    _bad_warning = (
                        "\n\nDO NOT use these imports — they failed:\n"
                        + "\n".join(f"  BROKEN: {imp}" for imp in _bad_imports)
                        + "\n\nOnly import from the modules listed in the IMPORT MAP "
                        "or from the source files shown in CURRENT CODE. "
                        "Test the code directly without going through an app factory "
                        "or entry-point unless one is shown in CURRENT CODE.\n"
                    )

                enriched_prompt = (
                    f"{prompt}{self._get_scaffold_context()}\n\n"
                    f"CURRENT CODE:\n"
                    + "\n".join(f"── {fp} ──\n```{self._code_fence_lang}\n{code}\n```" for fp, code in current_files.items())
                    + f"\n\nThe previous tests had collection errors:\n{error_detail}"
                    + _bad_warning
                    + "\nFix the imports and test structure."
                )
                try:
                    test_result = self._tester.generate_multi(
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
            except (AIInsufficientFunds, OrchestratorMaxIterationsError):
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
            test_result = self._tester.generate_multi(
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
            code_result = self._coder.generate_multi(
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
        """Code-first: unified code+test generation per file."""
        log(f"Orchestrator [CODE_FIRST]: generating code + tests for {len(target_files)} file(s)...")
        t0 = time.time()
        current_files = {}
        current_test_files = {}
        all_deps = []
        test_scaffold = ""

        # Pre-load dependency files from disk so the coder sees the actual
        # API of files built by prior issues (not just architecture descriptions).
        dep_context = {}
        if self._dependency_edges:
            target_fps = {tf["filepath"] for tf in target_files}
            visited = set()
            frontier = list(target_fps)
            while frontier:
                fp = frontier.pop()
                if fp in visited:
                    continue
                visited.add(fp)
                for edge in self._dependency_edges:
                    edge_src = edge.source_filepath if hasattr(edge, 'source_filepath') else edge.get('source_filepath', '')
                    edge_tgt = edge.target_filepath if hasattr(edge, 'target_filepath') else edge.get('target_filepath', '')
                    if edge_src == fp and edge_tgt not in dep_context and edge_tgt not in target_fps:
                        try:
                            dep_path = self._workspace.path(edge_tgt)
                            if dep_path.exists():
                                content = dep_path.read_text()
                                if content.strip() and not self._is_stub_file(edge_tgt, content):
                                    dep_context[edge_tgt] = content
                                    frontier.append(edge_tgt)
                        except Exception:
                            pass
            if dep_context:
                log(f"Orchestrator [CODE_FIRST]: loaded {len(dep_context)} dependency file(s) from prior issues")

        # Unified generation: one agent writes both code and tests together.
        # This ensures tests match the actual implementation (same types, APIs, etc).
        ordered_targets = _order_by_dependencies(target_files)
        test_file_set = set(test_files)
        for tf in ordered_targets:
            # Context: already-generated files + existing code + dependency files
            ctx_code = dict(dep_context)
            ctx_code.update(current_files)
            fp = tf["filepath"]
            if fp in existing_code:
                ctx_code[fp] = existing_code[fp]

            # Find corresponding test file(s) for this target
            # _find_tests_for_source expects a dict, but we only have paths here
            test_files_dict = {tfp: "" for tfp in test_files}
            related_tests_dict = _find_tests_for_source(fp, test_files_dict)
            related_tests = list(related_tests_dict.keys())
            if not related_tests:
                related_tests = list(test_file_set)[:1]  # fallback: first test file

            code_result = self._coder.generate_multi(
                issue_description=self._language_prefix + prompt + extra_context,
                target_files=[tf],
                architecture_context=architecture_context,
                existing_code=ctx_code,
                test_files=related_tests,
            )
            for ch in code_result.changes:
                if ch.filepath in test_file_set:
                    current_test_files[ch.filepath] = ch.code
                else:
                    current_files[ch.filepath] = ch.code
            all_deps.extend(code_result.dependencies)
            # Capture test scaffold from coder (last non-empty one wins)
            if code_result.test_scaffold:
                test_scaffold = code_result.test_scaffold
                self._test_scaffold = test_scaffold
        log(f"Orchestrator [CODE_FIRST]: unified generation done in {time.time() - t0:.1f}s "
            f"({len(current_files)} source + {len(current_test_files)} test files)")

        # Fallback: if coder didn't produce test files, fall back to tester
        missing_tests = [tf for tf in test_files if tf not in current_test_files]
        if missing_tests:
            log(f"Orchestrator [CODE_FIRST]: {len(missing_tests)} test file(s) missing — generating via tester...")
            t0 = time.time()
            for test_fp in missing_tests:
                related_source = _find_source_for_test(test_fp, current_files)
                if dep_context:
                    related_source = dict(dep_context, **related_source) if related_source else dict(dep_context)
                test_prompt = self._language_prefix + prompt + extra_context
                test_result = self._tester.generate_multi(
                    problem_statement=test_prompt,
                    test_files=[test_fp],
                    source_code=related_source,
                )
                for tf_out in test_result.test_files:
                    current_test_files[tf_out.filepath] = tf_out.tests
                all_deps.extend(test_result.dependencies)
            log(f"Orchestrator [CODE_FIRST]: fallback test generation done in {time.time() - t0:.1f}s")

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

            # Agentic diagnosis — only if enabled and we've hit the agentic debug threshold
            agentic_diagnosis = None
            if (self._enable_agentic_debug
                    and self._debugger_factory is not None
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

                    # Record diagnosis + fixes so the next debugger sees history
                    self._stall_detector.record_diagnosis(agentic_diagnosis)

                    # Bail if the debugger is proposing the same fix again
                    if self._stall_detector.is_duplicate_fix():
                        n_fixes = len(agentic_diagnosis.code_fixes)
                        log(
                            f"Orchestrator: debugger proposed the same fix twice "
                            f"({n_fixes} file(s)) — bailing on this ticket"
                        )
                        raise OrchestratorMaxIterationsError(
                            f"Debugger stuck: same fix proposed twice for "
                            f"{agentic_diagnosis.root_cause_category}"
                        )

                except AIInsufficientFunds:
                    raise
                except OrchestratorMaxIterationsError:
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

            # Escalate repair model — if the model actually changed,
            # give it a clean error-signature slate. If it didn't (already
            # at top tier), keep error signatures so the stall detector
            # fires immediately on the next identical failure.
            escalated = self._try_escalate_model(log, progression=self._repair_progression, agent="repair")
            self._stall_detector.reset_counters(keep_error_signatures=not escalated)

            # If the issue was purely a dependency problem, re-run without repair
            if (diagnosis
                    and diagnosis.root_cause_category == "dependency_issue"
                    and missing_packages):
                log("Orchestrator: dependency issue resolved — retrying tests...")
                return current_files, current_test_files, 0, None

            # Clear message histories to prevent token bloat after escalation
            self._coder.clear_message_history()
            self._tester.clear_message_history()

            # stall_recovery="none": escalate model only, no strategy flips or regeneration.
            # Fall through to the inline repair below (don't return early).
            if self._stall_recovery == "none":
                log("Orchestrator: stall recovery=none — falling through to inline repair")
                # Fall through — the inline repair at the bottom of this method will run

            # Strategy flip (stall_recovery="full" only): after 2 stall cycles, switch strategy.
            is_tdd = getattr(self, '_strategy', None) == CodingStrategy.TDD
            if self._stall_recovery == "full" and self._stall_cycle_count == 2 and is_tdd:
                log("Orchestrator: flipping from TDD to CODE_FIRST — tests can now be modified")
                self._strategy = CodingStrategy.CODE_FIRST
                # Regenerate tests from spec only (no source blob — architecture context is in the prompt)
                test_result = self._tester.generate_multi(
                    problem_statement=prompt + self._get_scaffold_context() + f"\n\nPrevious failures:\n{failure_output}",
                    test_files=test_files,
                    source_code=None,
                )
                new_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}
                return current_files, new_test_files, 0, None
            elif self._stall_recovery == "full" and self._stall_cycle_count == 2 and not is_tdd:
                log("Orchestrator: flipping from CODE_FIRST to TDD — tests become the spec")
                self._strategy = CodingStrategy.TDD
                # Regenerate tests from spec (TDD style, no source code)
                test_result = self._tester.generate_multi(
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
                    code_result = self._coder.generate_multi(
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

            # After multiple stall cycles without progress, do a full regeneration (full/regenerate only)
            if self._stall_recovery in ("full", "regenerate") and self._stall_cycle_count >= 3:
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
                code_result = self._coder.generate_multi(
                    issue_description=enriched_code_prompt,
                    target_files=target_files,
                    architecture_context=architecture_context,
                    existing_code={},
                )
                new_files = {ch.filepath: ch.code for ch in code_result.changes}
                # Update scaffold from fresh code generation
                if code_result.test_scaffold:
                    self._test_scaffold = code_result.test_scaffold
                test_result = self._tester.generate_multi(
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
                test_result = self._tester.generate_multi(
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
                    repaired = self._coder.repair_multi(
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
                repaired = self._coder.repair_multi(
                    current_files=all_files,
                    error_message=enriched_error,
                    architecture_context=architecture_context,
                )
                new_files, new_test_files = self._apply_multi_repair(
                    repaired, current_files, current_test_files,
                )
                new_hash = _hash("".join(sorted(f"{k}:{v}" for k, v in new_files.items())))
                return new_files, new_test_files, 0, new_hash

            # No diagnosis available — regenerate (full/regenerate only, not "none")
            if self._stall_recovery != "none":
                if is_tdd:
                    # TDD mode: regenerate code from scratch (tests are the spec)
                    log("Orchestrator: TDD stall — regenerating code from scratch...")
                    code_result = self._coder.generate_multi(
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
                    test_result = self._tester.generate_multi(
                        problem_statement=prompt + self._get_scaffold_context() + f"\n\nPrevious failures:\n{failure_output}",
                        test_files=test_files,
                        source_code=None,
                    )
                    new_test_files = {tf.filepath: tf.tests for tf in test_result.test_files}
                    return current_files, new_test_files, 0, None

            # stall_recovery="none": fall through to inline repair below

        # Inline repair with full dependency context
        # Extract failing pair + transitive deps from the graph
        relevant_files = _extract_failing_pair(
            failure_output, current_files, current_test_files,
            dependency_edges=self._dependency_edges,
        )

        # Determine which files are writable (current issue's target files + test files)
        writable_paths = set(tf["filepath"] for tf in target_files) | set(current_test_files.keys())

        # Universal project config files (jest.config, package.json, pyproject.toml,
        # Dockerfile, etc.) are always writable for repair, even when not declared in
        # target_files. Without this, the AI repeatedly identifies a missing test
        # preset or missing dependency manifest as the actual fix and gets blocked.
        # We load any that exist on disk, hand them to the coder as writable
        # source, and permit edits in the post-repair filter.
        for cfg_path in self._workspace.list_relative_files():
            cfg_str = str(cfg_path)
            if _is_config_file(cfg_str) and cfg_str not in writable_paths:
                try:
                    relevant_files[cfg_str] = self._workspace.path(cfg_str).read_text()
                    writable_paths.add(cfg_str)
                except Exception:
                    pass

        # Load dependency files from disk that the current issue's code imports from.
        # These are files from prior issues that are already on disk but NOT in
        # current_files (which only tracks the current issue's target files).
        # Strategy: use dependency graph edges AND scan actual imports in source code.
        _dep_loaded = set()

        if self._dependency_edges:
            source_fps = set(tf["filepath"] for tf in target_files)
            visited = set()
            frontier = list(source_fps)
            while frontier:
                fp = frontier.pop()
                if fp in visited:
                    continue
                visited.add(fp)
                for edge in self._dependency_edges:
                    edge_src = edge.source_filepath if hasattr(edge, 'source_filepath') else edge.get('source_filepath', '')
                    edge_tgt = edge.target_filepath if hasattr(edge, 'target_filepath') else edge.get('target_filepath', '')
                    if edge_src == fp and edge_tgt not in relevant_files and edge_tgt not in visited:
                        try:
                            dep_path = self._workspace.path(edge_tgt)
                            if dep_path.exists():
                                relevant_files[edge_tgt] = dep_path.read_text()
                                _dep_loaded.add(edge_tgt)
                                frontier.append(edge_tgt)
                        except Exception:
                            pass

        # Also scan actual imports in source files to catch deps not in the graph
        for fp, content in list(current_files.items()):
            for imp_match in re.finditer(
                r'^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))',
                content, re.MULTILINE,
            ):
                module_path = (imp_match.group(1) or imp_match.group(2) or "")
                # Convert dotted module path to file path
                parts = module_path.split(".")
                for i in range(len(parts), 0, -1):
                    candidate = "/".join(parts[:i]) + ".py"
                    if candidate not in relevant_files and candidate not in _dep_loaded:
                        try:
                            dep_path = self._workspace.path(candidate)
                            if dep_path.exists() and candidate not in writable_paths:
                                relevant_files[candidate] = dep_path.read_text()
                                _dep_loaded.add(candidate)
                                break
                        except Exception:
                            pass

        # Separate into writable source, writable tests, and read-only context
        repair_source = {}
        repair_tests = {}
        readonly_context = {}
        for fp, content in relevant_files.items():
            if fp in current_test_files:
                repair_tests[fp] = content
            elif fp in writable_paths:
                repair_source[fp] = content
            else:
                readonly_context[fp] = content

        log(f"Orchestrator: inline repair with {len(repair_source)} source + {len(repair_tests)} test"
            f" + {len(readonly_context)} readonly file(s)...")

        # Truncate error output — keep head (summary) + tail (most relevant failures)
        truncated_error = _truncate_error(failure_output)

        # Inject preflight "did you mean?" hints so the repair LLM knows
        # the correct import paths instead of guessing the same wrong ones.
        preflight_issues = getattr(self, "_preflight_issues", None)
        if preflight_issues:
            hints = "\n".join(f"  - {issue}" for issue in preflight_issues)
            truncated_error = (
                f"=== Import resolution hints (from preflight) ===\n"
                f"{hints}\n\n"
                f"=== Error output ===\n"
                f"{truncated_error}"
            )

        # If previous repair had read-only changes filtered, warn the LLM
        if self._readonly_filter_warning:
            truncated_error = self._readonly_filter_warning + "\n" + truncated_error

        t0 = time.time()
        repaired = self._coder.repair_multi_inline(
            source_files=repair_source,
            test_files=repair_tests,
            error_message=truncated_error,
            readonly_context=readonly_context,
        )
        log(f"Orchestrator: repair done in {time.time() - t0:.1f}s")

        # Filter out any changes to read-only files (LLM may ignore the instruction)
        repaired_changes = [
            ch for ch in repaired.changes
            if ch.filepath in writable_paths
        ]
        if len(repaired_changes) < len(repaired.changes):
            skipped_files = [
                ch.filepath for ch in repaired.changes
                if ch.filepath not in writable_paths
            ]
            log(f"Orchestrator: filtered {len(skipped_files)} change(s) to read-only files")
            self._readonly_filter_warning = (
                f"\n\nIMPORTANT: Your previous repair attempted to modify these READ-ONLY files "
                f"(from prior issues), but those changes were REJECTED:\n"
                + "\n".join(f"  - {fp}" for fp in skipped_files)
                + "\n\nYou CANNOT change these files. You MUST adapt your writable source "
                "and test files to work with the read-only API exactly as it is. "
                "Read the read-only files carefully and match their actual interface "
                "(field names, types, method signatures).\n"
            )
        else:
            self._readonly_filter_warning = ""
        from bizniz.agents.coder.types import CoderProcessResult
        repaired = CoderProcessResult(changes=repaired_changes, dependencies=repaired.dependencies)

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

    def _get_passing_tests(self, log: Callable, restrict_to: Optional[Set[str]] = None) -> set:
        """
        Run all existing tests in the workspace and return the set of
        test file paths that pass. Used as a baseline for regression detection.

        restrict_to: if provided, only check these specific test file paths.
            This prevents scaffold stubs from future issues being included
            in the regression baseline.
        """
        try:
            if restrict_to is not None:
                test_paths = [tp for tp in restrict_to if self._workspace.exists(path=tp)]
            else:
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

        If the coder generates e.g. 'pkg/models.py' but a directory
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

    # ── Source import error detection and auto-fix ────────────────────────────

    def _is_source_import_error(self, error_detail: str, current_files: dict) -> bool:
        """
        Detect if a collection error is caused by broken code in our source
        files (not the test file). If True, the orchestrator repairs the
        source; if False, it regenerates the test.

        Triple-check, in order of strength:

          1. **Traceback frame match.** If the pytest traceback includes a
             frame located inside any of the workspace's source files
             (``<filepath>:<line>:`` style markers, or ``File "<filepath>"``),
             the failure is happening inside our code regardless of what the
             test tried to import. This catches NameError / AttributeError /
             SyntaxError surfacing during import.

          2. **Failing-import module match.** The first ``> from X import``
             line in the error matches one of our source modules. Catches
             the classic "test imports our broken module" case.

          3. **Source-file path mention.** The error text references any of
             our workspace source paths verbatim. Cheapest fallback.

        Any positive signal returns True. All three negative → False (test
        is the problem, regenerate it).
        """
        import re

        if not error_detail:
            return False

        # Build module paths AND filepath strings from our source files
        our_modules: set = set()
        our_paths: set = set()
        for fp in current_files:
            our_paths.add(fp)
            module = fp.replace("/", ".").replace(".py", "")
            our_modules.add(module)
            parts = module.split(".")
            if len(parts) >= 2:
                our_modules.add(".".join(parts[-2:]))

        # ── Signal 1: traceback frames pointing into our source files ───────
        # Match common formats:
        #   /workspace/pet_groomer/app.py:7: NameError
        #   File "/workspace/pet_groomer/app.py", line 7
        #   pet_groomer/app.py:7: SomeError
        for source_path in our_paths:
            # Skip test files when matching frames — those are the test
            # framework's own report on test code, not "source broke".
            if source_path.startswith("tests/") or "/tests/" in source_path:
                continue
            if source_path in error_detail:
                # Check it's used in a frame-like context (line number nearby)
                # to avoid false positives where the path appears only in a
                # repair-prompt header.
                pattern = re.escape(source_path) + r'(?:":?\s*,?\s*line\s*\d+|:\d+:)'
                if re.search(pattern, error_detail):
                    return True

        # ── Signal 2: first failing import line references our module ──────
        first_match = re.search(r'>\s*(?:from\s+(\S+)\s+import|import\s+(\S+))', error_detail)
        failing_imports = [first_match.groups()] if first_match else []
        if not failing_imports:
            first_match = re.search(r'(?:from\s+(\S+)\s+import|import\s+(\S+))', error_detail)
            failing_imports = [first_match.groups()] if first_match else []

        for groups in failing_imports:
            imported_module = groups[0] or groups[1]
            if not imported_module:
                continue
            for our_mod in our_modules:
                if imported_module == our_mod or imported_module.endswith("." + our_mod):
                    return True

        # ── Signal 3: any of our source paths mentioned (last resort) ──────
        # Only counts if the path is non-test and the error was an
        # ImportError/ModuleNotFoundError (otherwise we'd over-trigger on
        # paths in repair-prompt fragments).
        is_import_failure = any(
            phrase in error_detail
            for phrase in ("ImportError:", "ModuleNotFoundError:")
        )
        if is_import_failure:
            for source_path in our_paths:
                if source_path.startswith("tests/") or "/tests/" in source_path:
                    continue
                if source_path in error_detail:
                    return True

        return False

    def _auto_fix_source_imports(
        self, current_files: dict, error_detail: str, log: Callable,
    ) -> bool:
        """
        Attempt to fix broken imports in source files by scanning the actual
        workspace for the correct module paths. This handles the common case
        where the coder generates 'from pkg.api.models.X import Y' but
        the actual module is at 'from pkg.models.X import Y'.

        Returns True if any fixes were applied.
        """
        import ast
        import re

        # Build a map of what actually exists in the workspace
        try:
            rel_files = self._workspace.list_relative_files()
        except Exception:
            return False

        existing_modules = set()
        for p in rel_files:
            p_str = str(p)
            if p_str.endswith(".py") and not p_str.startswith("."):
                module = p_str.replace("/", ".").replace(".py", "")
                existing_modules.add(module)
                # Also add the package path (for 'from pkg.models import X')
                if "." in module:
                    existing_modules.add(module.rsplit(".", 1)[0])

        fixed_any = False
        for fp, code in list(current_files.items()):
            if not fp.endswith(".py"):
                continue
            try:
                tree = ast.parse(code, filename=fp)
            except SyntaxError:
                continue

            new_code = code
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module
                    level = node.level or 0

                    # Resolve relative imports to absolute module path
                    if level > 0:
                        # Compute the package of the current file
                        fp_module = fp.replace("/", ".").replace(".py", "")
                        parts = fp_module.split(".")
                        # Go up `level` packages from the file's directory
                        # (the file's package is its parent dir)
                        pkg_parts = parts[:-1]  # remove filename
                        if level <= len(pkg_parts):
                            base = ".".join(pkg_parts[:len(pkg_parts) - level + 1])
                        else:
                            base = ""
                        resolved = f"{base}.{mod}" if base else mod
                    else:
                        resolved = mod

                    if resolved in existing_modules:
                        continue  # import is valid

                    # Try to find the correct module by searching for the
                    # leaf name in existing modules
                    leaf = resolved.split(".")[-1]
                    candidates = [
                        m for m in existing_modules
                        if m.endswith(f".{leaf}") or m == leaf
                    ]

                    # If multiple candidates, filter out auto-generated stubs
                    if len(candidates) > 1:
                        real_candidates = []
                        for c in candidates:
                            c_path = c.replace(".", "/") + ".py"
                            content = None
                            try:
                                content = self._workspace.read_file(c_path)
                            except Exception:
                                pass
                            if content and "Auto-generated stub" not in content:
                                real_candidates.append(c)
                        if real_candidates:
                            candidates = real_candidates

                    if len(candidates) == 1:
                        correct = candidates[0]
                        # For relative imports, rewrite as absolute import
                        if level > 0:
                            dots = "." * level
                            old_import = f"from {dots}{mod}"
                            new_import = f"from {correct}"
                            log(f"Orchestrator: auto-fixed relative import {dots}{mod} → {correct} in {fp}")
                        else:
                            old_import = f"from {mod}"
                            new_import = f"from {correct}"
                            log(f"Orchestrator: auto-fixed import {mod} → {correct} in {fp}")
                        if old_import in new_code:
                            new_code = new_code.replace(old_import, new_import)
                            # Clean up auto-stub at the old (wrong) path
                            old_path = resolved.replace(".", "/") + ".py"
                            if old_path in current_files:
                                old_content = current_files[old_path]
                                if "Auto-generated stub" in old_content:
                                    del current_files[old_path]
                                    try:
                                        self._workspace.delete_file(old_path)
                                        log(f"Orchestrator: removed stale stub {old_path}")
                                    except Exception:
                                        pass

            if new_code != code:
                current_files[fp] = new_code
                self._workspace.write_file(path=fp, content=new_code)
                fixed_any = True

        return fixed_any

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
            Which agent(s) to update: "coder", "tester", "repair", or "all".
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
                    self._coder._client = fresh_client
                    self._tester._client = fresh_client
                    if self._quick_debugger:
                        self._quick_debugger._client = fresh_client
                    _retag_client_for_agent(fresh_client, self._coder)
                    _retag_client_for_agent(fresh_client, self._tester)
                    if self._quick_debugger:
                        _retag_client_for_agent(fresh_client, self._quick_debugger)
                elif agent == "coder":
                    self._coder._client = fresh_client
                    _retag_client_for_agent(fresh_client, self._coder)
                elif agent == "tester":
                    self._tester._client = fresh_client
                    _retag_client_for_agent(fresh_client, self._tester)
                elif agent == "repair":
                    self._coder._client = fresh_client  # repair uses coder
                    _retag_client_for_agent(fresh_client, self._coder)
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
        """Install a package into the test environment and persist to DB.

        Validates the package name before installing: rejects stdlib modules,
        malformed names, and packages that don't exist on PyPI. Deduplicates
        against requirements.txt using normalized names (lowercase, - → _).
        """
        import re as _re
        import sys as _sys

        # Strip version specifiers for validation
        bare = _re.split(r"[><=!\[;]", package)[0].strip()
        bare_lower = bare.lower().replace("-", "_")

        # Reject empty or malformed names
        if not bare or not _re.match(r"^(@[a-zA-Z0-9._-]+/)?[a-zA-Z][a-zA-Z0-9._-]*$", bare):
            log(f"Orchestrator: rejected invalid package name '{package}'")
            return

        # Reject stdlib modules
        if self._lang.is_stdlib(bare_lower):
            log(f"Orchestrator: rejected stdlib module '{package}'")
            return

        # Check for duplicates in requirements.txt / package.json BEFORE install
        existing_pkgs = set()
        installed_str = self._lang.get_installed_packages(self._workspace)
        if installed_str:
            for line in installed_str.splitlines():
                pkg_name = _re.split(r"[><=!\[;@]", line.strip())[0].strip()
                existing_pkgs.add(pkg_name.lower().replace("-", "_"))

        if bare_lower in existing_pkgs:
            return  # already declared

        # Verify package exists before installing
        # Skip PyPI check for npm packages (scoped @org/pkg or JS/TS project)
        is_npm = bare.startswith("@") or self._lang.name != "python"
        if not is_npm:
            from bizniz.preflight.python_validator import _pypi_package_exists
            exists = _pypi_package_exists(bare)
            if exists is False:
                log(f"Orchestrator: rejected '{package}' — not found on PyPI")
                return
            # If exists is None (network error), proceed cautiously

        env = self._find_installable_environment()
        if env:
            # install_packages() handles both container install and requirements.txt update
            env.install_packages([package])
            log(f"Orchestrator: installed package '{package}'")
        else:
            log(f"Orchestrator: WARNING — no installable environment found for '{package}'")
            # No environment — still record in requirements.txt for future builds
            try:
                req_path = self._workspace.path("requirements.txt")
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
        if hasattr(self._coder, '_environment') and hasattr(self._coder._environment, 'install_packages'):
            return self._coder._environment
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

    def _get_installed_packages(self) -> str:
        """Read installed packages using the language strategy."""
        return self._lang.get_installed_packages(self._workspace)

    _SKIP_DIRS = {"node_modules", "__pycache__", ".bizniz", "dist", "build", ".next", ".git", "bin", "obj"}

    @classmethod
    def _should_skip_path(cls, rel_path: str) -> bool:
        """Return True if path is inside a directory that should be excluded from analysis."""
        parts = rel_path.replace("\\", "/").split("/")
        return any(p in cls._SKIP_DIRS or p.startswith(".") for p in parts[:-1])

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
            if rel_path.startswith(".") or self._should_skip_path(rel_path):
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
            elif not self._is_stub_file(rel_path, content):
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
                if self._is_stub_class(node):
                    continue  # skip stubs entirely
                methods = []
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        args = ", ".join(a.arg for a in item.args.args)
                        methods.append(f"{item.name}({args})")
                methods_str = f" [{', '.join(methods[:5])}]" if methods else ""
                signatures.append(f"class {node.name}{methods_str}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if self._is_stub_function(node):
                    continue  # skip stubs entirely
                args = ", ".join(a.arg for a in node.args.args)
                signatures.append(f"def {node.name}({args})")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        signatures.append(target.id)

        return signatures[:10]

    def _build_import_map(self) -> str:
        """Build explicit import statements for every workspace module.

        Returns a string like:
          from pet_groomer.models.service import Service
          from pet_groomer.errors import ServiceNotFoundError, SlotConflictError
          from pet_groomer.repositories.in_memory_services import InMemoryServicesRepository

        This eliminates import guessing — agents use these exact paths.
        """
        import ast

        lines = []
        try:
            rel_files = self._workspace.list_relative_files()
        except Exception:
            return ""

        for rel_path in sorted(str(p) for p in rel_files):
            if rel_path.startswith(".") or self._should_skip_path(rel_path):
                continue
            if not rel_path.endswith(".py"):
                continue
            # Skip test files, __init__.py, and config files
            if rel_path.startswith("tests") or rel_path == "conftest.py":
                continue
            if rel_path.endswith("__init__.py"):
                continue

            # Convert filepath to module path
            module = rel_path.replace("/", ".").replace(".py", "")

            try:
                content = self._workspace.read_file(path=rel_path)
            except Exception:
                continue

            if not content or not content.strip():
                continue

            # Extract top-level names
            try:
                tree = ast.parse(content, filename=rel_path)
            except SyntaxError:
                continue

            names = []
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_") and not self._is_stub_function(node):
                        names.append(node.name)
                elif isinstance(node, ast.ClassDef):
                    if not node.name.startswith("_") and not self._is_stub_class(node):
                        names.append(node.name)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and not target.id.startswith("_"):
                            names.append(target.id)

            if names:
                lines.append(f"  from {module} import {', '.join(names[:10])}")
            elif any(
                isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                for n in ast.iter_child_nodes(tree)
            ):
                # File has definitions but they're all stubs — skip entirely
                continue
            else:
                lines.append(f"  import {module}")

        return "\n".join(lines) if lines else ""

    def _scope_architecture_context(
        self, arch_context: str, target_files: List[dict],
    ) -> str:
        """Filter architecture context to only reference relevant modules.

        Keeps lines about target files and their upstream dependencies.
        Removes lines about unrelated modules (e.g. app.py when we're
        building a router) so the LLM doesn't try to import from them.
        """
        if not arch_context or not target_files:
            return arch_context

        # Collect filepaths that are relevant: targets + upstream deps
        relevant_files = {tf["filepath"] for tf in target_files}
        if self._dependency_edges:
            # Walk upstream: if our target imports from X, X is relevant
            changed = True
            while changed:
                changed = False
                for edge in self._dependency_edges:
                    src = edge.source_filepath if hasattr(edge, "source_filepath") else edge.get("source_filepath", "")
                    tgt = edge.target_filepath if hasattr(edge, "target_filepath") else edge.get("target_filepath", "")
                    if src in relevant_files and tgt not in relevant_files:
                        relevant_files.add(tgt)
                        changed = True

        # Filter the formatted context line by line.
        # Architecture context has sections like:
        #   - pet_groomer_backend/app.py: ...
        #     def create_app() -> FastAPI
        # We drop module blocks whose filepath isn't relevant.
        filtered_lines = []
        skip_block = False
        for line in arch_context.splitlines():
            # Detect module filepath references (indented with "  - ")
            stripped = line.strip()
            if stripped.startswith("- ") and "(" in stripped and "/" in stripped:
                # Extract filepath from patterns like "- ClassName (path/to/file.py)"
                # or "- (module-level) (path/to/file.py)"
                import re
                match = re.search(r'\(([^)]+\.\w+)\)', stripped)
                if match:
                    filepath = match.group(1)
                    skip_block = filepath not in relevant_files
                else:
                    skip_block = False
            elif stripped.startswith("- ") and " → " in stripped:
                # Dependency line like "- app.py → routers/services.py [*]"
                # Only show if the source is relevant (what our code imports FROM)
                src_path = stripped.split(" → ")[0].lstrip("- ").strip()
                skip_block = src_path not in relevant_files
            elif not line.startswith("    ") and not line.startswith("\t"):
                # Section header (e.g. "Modules:", "Dependencies:") — always keep
                skip_block = False

            if not skip_block:
                filtered_lines.append(line)

        return "\n".join(filtered_lines)

    def _is_stub_file(self, filepath: str, content: str) -> bool:
        """Check if a file contains only stubs (no real implementations).

        Also treats files with syntax errors or broken imports as stubs —
        they can't be imported safely either.
        """
        import ast
        if not filepath.endswith(".py"):
            return False
        try:
            tree = ast.parse(content, filename=filepath)
        except SyntaxError:
            return True  # unparseable = unsafe to import from
        definitions = [
            n for n in ast.iter_child_nodes(tree)
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if not definitions:
            return False  # no definitions = utility/config file, not a stub
        return all(
            (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and self._is_stub_function(n))
            or (isinstance(n, ast.ClassDef) and self._is_stub_class(n))
            for n in definitions
        )

    def _is_stub_class(self, node) -> bool:
        """Check if a ClassDef is a scaffold stub (all methods are stubs or empty)."""
        import ast
        methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if not methods:
            # Class with no methods — check if body is just pass/docstring
            meaningful = [
                stmt for stmt in node.body
                if not (isinstance(stmt, ast.Pass)
                        or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                            and isinstance(stmt.value.value, str)))
            ]
            return len(meaningful) == 0
        return all(self._is_stub_function(m) for m in methods)

    def _is_stub_function(self, node) -> bool:
        """Check if a FunctionDef is a scaffold stub (body is only raise/pass/docstring+raise)."""
        import ast
        # Filter out docstrings and pass statements to find meaningful body
        meaningful = [
            stmt for stmt in node.body
            if not (isinstance(stmt, ast.Pass)
                    or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)))
        ]
        if len(meaningful) == 0:
            return True  # only pass/docstring — stub
        if len(meaningful) == 1 and isinstance(meaningful[0], ast.Raise):
            exc = meaningful[0].exc
            if exc is None:
                return True  # bare raise
            # raise NotImplementedError / raise NotImplementedError(...)
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                return True
            if isinstance(exc, ast.Call):
                func = exc.func
                if isinstance(func, ast.Name) and func.id == "NotImplementedError":
                    return True
        return False

    def _get_passing_test_examples(self, max_examples: int = 2) -> str:
        """
        Get content of existing passing test files as examples for the tester.

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
        validator = get_validator(self._lang.name, self._workspace)
        if validator is None:
            return current_files

        result = validator.validate(current_files, declared_dependencies)

        # Remove shadow files from current_files
        for sf in result.shadow_files_removed:
            current_files.pop(sf, None)

        # Write rewritten files to workspace (relative → absolute imports)
        rewritten_files = {rw.filepath for rw in result.import_rewrites}
        for fp in rewritten_files:
            if fp in current_files:
                self._workspace.write_file(path=fp, content=current_files[fp])

        # Write stubs to workspace
        for stub in result.stubs_created:
            self._workspace.write_file(path=stub.filepath, content=stub.content)
            current_files[stub.filepath] = stub.content

        # Install packages identified by PyPI lookup
        if result.packages_to_install:
            env = self._find_installable_environment()
            if env:
                for pkg in result.packages_to_install:
                    try:
                        env.install_packages([pkg])
                        log(f"pip installed {pkg} (detected via PyPI)")
                    except Exception:
                        log(f"failed to pip install {pkg}")

        if result.import_rewrites or result.stubs_created or result.issues or result.shadow_files_removed or result.packages_to_install:
            log(result.summary())

        # Store unresolved issues so the repair loop can inject
        # "did you mean?" hints into the error context.
        self._preflight_issues = result.issues

        return current_files

    # ── Container import validation ───────────────────────────────────────────

    def _validate_imports_in_container(
        self,
        current_files: dict,
        current_test_files: dict,
        architecture_context: str,
        log: Callable,
    ) -> dict:
        """Batch-validate all imports inside the Docker container.

        Runs after preflight (relative→absolute normalization) and before tests.
        Auto-fixes wrong paths, pip-installs missing deps, and asks the agent
        about ambiguous cases — all before the first test iteration.
        """
        if self._lang.name != "python":
            return current_files

        env = self._find_installable_environment()
        if env is None or not hasattr(env, '_container_id'):
            return current_files

        # 1. Collect all imports from source + test files
        all_code_files = {**current_files, **current_test_files}
        imports = self._collect_all_imports(all_code_files)
        if not imports:
            return current_files

        # 2. Batch-try imports in the container
        try:
            failures = self._batch_try_imports_in_container(imports, env, log)
        except Exception as exc:
            log(f"Orchestrator: container import validation skipped ({exc})")
            return current_files

        if not failures:
            return current_files

        log(f"Orchestrator: {len(failures)} import(s) failed in container")

        # 3. Build workspace export index (static AST on host)
        export_index = self._build_workspace_export_index()
        workspace_modules = set(export_index.keys())

        # 4. Triage each failure
        auto_fixes = []
        pip_installs = []
        ambiguous = []
        for failure in failures:
            category, detail = self._triage_import_failure(
                failure, export_index, workspace_modules,
            )
            if category == "auto_fix":
                auto_fixes.append(detail)
            elif category == "pip_install":
                pip_installs.append(detail)
            elif category == "ambiguous":
                ambiguous.append(detail)

        # 5. Apply auto-fixes
        if auto_fixes:
            current_files = self._apply_import_auto_fixes(
                auto_fixes, current_files, current_test_files, log,
            )

        # 6. Batch pip-install missing packages
        if pip_installs:
            pkg_names = list({d["package"] for d in pip_installs})
            log(f"Orchestrator: pip-installing {len(pkg_names)} missing package(s): {', '.join(pkg_names)}")
            self._install_declared_dependencies(pkg_names, log)

        # 7. Resolve ambiguous cases via one LLM call
        if ambiguous:
            current_files = self._resolve_ambiguous_imports(
                ambiguous, current_files, architecture_context, log,
            )

        return current_files

    def _collect_all_imports(self, files: dict) -> list:
        """Extract all absolute imports from Python files.

        Returns list of dicts with module, names, filepath, raw_line.
        Skips stdlib and relative imports (already normalized by preflight).
        """
        import ast
        import sys

        stdlib = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else set()
        seen = set()
        result = []

        for filepath, content in files.items():
            if not filepath.endswith(".py"):
                continue
            try:
                tree = ast.parse(content, filename=filepath)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    top = node.module.split(".")[0]
                    if top in stdlib:
                        continue
                    names = [a.name for a in (node.names or []) if a.name != "*"]
                    key = (node.module, tuple(sorted(names)))
                    if key in seen:
                        continue
                    seen.add(key)
                    result.append({
                        "filepath": filepath,
                        "module": node.module,
                        "names": names,
                    })
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top in stdlib:
                            continue
                        key = (alias.name, ())
                        if key in seen:
                            continue
                        seen.add(key)
                        result.append({
                            "filepath": filepath,
                            "module": alias.name,
                            "names": [],
                        })

        return result

    def _batch_try_imports_in_container(
        self, imports: list, env, log: Callable,
    ) -> list:
        """Generate a Python script that tries every import and run it in Docker.

        Returns list of failure dicts: {module, names, error}.
        """
        import json as _json
        import subprocess as _sp

        # Ensure container is up and workspace is synced
        env._ensure_container()
        env._sync_workspace()

        # Build the test script
        lines = [
            "import json, sys",
            "failures = []",
        ]
        for imp in imports:
            mod = imp["module"]
            names = imp["names"]
            # Sanitize: only allow valid Python identifiers
            if not all(part.isidentifier() for part in mod.split(".")):
                continue
            if names and all(n.isidentifier() for n in names):
                names_str = ", ".join(names)
                stmt = f"from {mod} import {names_str}"
            else:
                stmt = f"import {mod}"
            lines.append("try:")
            lines.append(f"    {stmt}")
            lines.append("except (ImportError, ModuleNotFoundError) as e:")
            lines.append(
                f'    failures.append({{"module": {mod!r}, '
                f'"names": {[n for n in names]!r}, '
                f'"error": str(e)}})'
            )
            lines.append("except Exception:")
            lines.append("    pass  # runtime error in module body, not an import issue")

        lines.append("print(json.dumps(failures))")
        script = "\n".join(lines)

        proc = _sp.run(
            ["docker", "exec", env._container_id, "python3", "-c", script],
            capture_output=True, text=True, timeout=30,
        )

        # Try to parse results even if return code is non-zero —
        # the script may have printed partial results before an error.
        stdout = proc.stdout.strip() if proc.stdout else ""
        if proc.returncode != 0 and not stdout:
            log(f"Orchestrator: container import check script error: {proc.stderr[:200]}")
            return []

        try:
            return _json.loads(stdout)
        except (_json.JSONDecodeError, ValueError):
            if proc.returncode != 0:
                log(f"Orchestrator: container import check script error: {proc.stderr[:200]}")
            else:
                log("Orchestrator: container import check returned invalid JSON")
            return []

    def _build_workspace_export_index(self) -> dict:
        """AST-scan workspace files to build {module_path: set(defined_names)}.

        Runs on the host (no container needed). Only scans top-level definitions.
        """
        import ast

        index = {}
        try:
            rel_files = self._workspace.list_relative_files()
        except Exception:
            return index

        for p in rel_files:
            p_str = str(p)
            if not p_str.endswith(".py") or p_str.startswith("."):
                continue

            # Convert filepath to module path
            if p_str.endswith("/__init__.py"):
                module = p_str.replace("/__init__.py", "").replace("/", ".")
            else:
                module = p_str.replace("/", ".").replace(".py", "")

            try:
                content = self._workspace.read_file(p_str)
            except Exception:
                continue

            try:
                tree = ast.parse(content, filename=p_str)
            except SyntaxError:
                continue

            names = set()
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    names.add(node.name)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)

            index[module] = names

        return index

    def _triage_import_failure(
        self,
        failure: dict,
        export_index: dict,
        workspace_modules: set,
    ) -> tuple:
        """Categorize an import failure as auto_fix, pip_install, or ambiguous.

        Returns (category, detail_dict).
        """
        mod = failure["module"]
        names = failure.get("names", [])
        leaf = mod.split(".")[-1]
        top = mod.split(".")[0]

        # Check if the top-level package exists in workspace at all
        top_in_workspace = any(
            m == top or m.startswith(f"{top}.") for m in workspace_modules
        )

        if not top_in_workspace:
            # Not our code — try pip install
            from bizniz.preflight.python_validator import _COMMON_ALIASES
            pip_name = _COMMON_ALIASES.get(top, top)
            return ("pip_install", {"package": pip_name})

        # Our code but wrong path — search by leaf + exported names
        # First pass: match leaf AND all requested names
        exact_candidates = []
        for m, exports in export_index.items():
            m_leaf = m.split(".")[-1]
            if m_leaf == leaf and (not names or all(n in exports for n in names)):
                exact_candidates.append(m)

        if len(exact_candidates) == 1:
            return ("auto_fix", {
                "old_module": mod,
                "new_module": exact_candidates[0],
            })

        # Second pass: match leaf only (ignore names)
        if not exact_candidates:
            leaf_candidates = [
                m for m in workspace_modules
                if m.split(".")[-1] == leaf
            ]
            if len(leaf_candidates) == 1:
                return ("auto_fix", {
                    "old_module": mod,
                    "new_module": leaf_candidates[0],
                })
            if len(leaf_candidates) > 1:
                # Multiple leaf matches — build candidate info for the agent
                candidates = []
                for c in leaf_candidates:
                    exports = export_index.get(c, set())
                    candidates.append({
                        "module": c,
                        "exports": sorted(exports)[:20],
                    })
                return ("ambiguous", {
                    "failure": failure,
                    "candidates": candidates,
                })

        # Exact candidates > 1 — ambiguous with name matches
        if len(exact_candidates) > 1:
            candidates = []
            for c in exact_candidates:
                exports = export_index.get(c, set())
                candidates.append({
                    "module": c,
                    "exports": sorted(exports)[:20],
                })
            return ("ambiguous", {
                "failure": failure,
                "candidates": candidates,
            })

        # No matches at all — might be a deeply nested path issue
        # Try broader search: any module containing the leaf somewhere
        broad_candidates = [
            m for m in workspace_modules
            if f".{leaf}" in m or m == leaf
        ]
        if len(broad_candidates) == 1:
            return ("auto_fix", {
                "old_module": mod,
                "new_module": broad_candidates[0],
            })

        # Give up — treat as pip install (maybe a transitive dep)
        return ("pip_install", {"package": top})

    def _apply_import_auto_fixes(
        self,
        auto_fixes: list,
        current_files: dict,
        current_test_files: dict,
        log: Callable,
    ) -> dict:
        """Rewrite import paths in source files and write to workspace."""
        for fix in auto_fixes:
            old_mod = fix["old_module"]
            new_mod = fix["new_module"]
            old_text = f"from {old_mod}"
            new_text = f"from {new_mod}"

            # Fix in source files
            for fp in list(current_files.keys()):
                if old_text in current_files[fp]:
                    current_files[fp] = current_files[fp].replace(old_text, new_text)
                    self._workspace.write_file(path=fp, content=current_files[fp])
                    log(f"Orchestrator: import fix {old_mod} → {new_mod} in {fp}")

            # Fix in test files too
            for fp in list(current_test_files.keys()):
                if old_text in current_test_files[fp]:
                    current_test_files[fp] = current_test_files[fp].replace(
                        old_text, new_text,
                    )
                    self._workspace.write_file(
                        path=fp, content=current_test_files[fp],
                    )
                    log(f"Orchestrator: import fix {old_mod} → {new_mod} in {fp}")

        return current_files

    def _resolve_ambiguous_imports(
        self,
        ambiguous: list,
        current_files: dict,
        architecture_context: str,
        log: Callable,
    ) -> dict:
        """Send one batched message to the repair agent for all ambiguous imports."""
        if not ambiguous:
            return current_files

        lines = [
            "The following imports have AMBIGUOUS resolution — each has multiple "
            "workspace modules that could be the correct target. Fix the import "
            "statements in the source files to use the correct module path.\n",
        ]
        for i, case in enumerate(ambiguous, 1):
            fail = case["failure"]
            candidates = case["candidates"]
            names_str = ", ".join(fail["names"]) if fail["names"] else "*"
            lines.append(
                f"{i}. `from {fail['module']} import {names_str}` "
                f"(error: {fail['error']})"
            )
            for c in candidates:
                exports_str = ", ".join(c["exports"][:10])
                lines.append(f"   - {c['module']} (exports: {exports_str})")
            lines.append("")

        log(f"Orchestrator: asking agent to resolve {len(ambiguous)} ambiguous import(s)")

        try:
            repaired = self._coder.repair_multi(
                current_files=current_files,
                error_message="\n".join(lines),
                architecture_context=architecture_context,
            )
            for ch in repaired.changes:
                if ch.filepath in current_files:
                    current_files[ch.filepath] = ch.code
                    self._workspace.write_file(
                        path=ch.filepath, content=ch.code,
                    )
                    log(f"Orchestrator: agent resolved import in {ch.filepath}")
        except Exception as exc:
            log(f"Orchestrator: ambiguous import resolution failed ({exc})")

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
        imports = self._lang.scan_imports(all_files)
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

        third_party = self._lang.filter_third_party(imports, workspace_modules)
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
        Use the QuickDebugger to diagnose the failure and decide whether to
        repair code or regenerate tests.

        Returns (current_code, current_tests, stale_count, previous_code_hash).
        """
        log("Orchestrator: running quick_debugger diagnosis...")

        try:
            diagnosis = self._quick_debugger.diagnose(
                error_output=failure_output,
                code=current_code,
                code_filename=code_filename,
                test_code=current_tests,
                test_filename=test_filename,
            )
        except AIInsufficientFunds:
            raise
        except Exception as e:
            log(f"Orchestrator: quick_debugger failed ({e}), falling back to code repair...")
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
            test_result = self._tester.process_from_prompt(
                prompt=regen_prompt,
                output_path=test_filename,
                code_filename=code_filename,
            )
            return current_code, _extract_tests(test_result.test_files, test_filename), 0, None

        else:
            # fix_target == "code"
            enriched_error = (
                f"QUICK DEBUGGER DIAGNOSIS:\n"
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

            repaired = self._coder.repair(
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
        Original heuristic-based failure handling (no quick_debugger).

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
                test_result = self._tester.process_from_prompt(
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
        repaired = self._coder.repair(
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
        repaired = self._coder.repair(
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

    # ── Public repair helper (also used by Engineer) ──────────────────────

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
        self._tester.review_tests(
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
    dependency_edges: Optional[list] = None,
) -> dict:
    """
    Extract the failing test file + the source file it tests + transitive deps.

    When dependency_edges (from ArchitecturePlan) are available, uses exact
    graph lookups to find the full dependency chain. Falls back to string
    matching via _include_transitive_deps when edges aren't available.
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
    primary_source_fp = None
    for code_fp in current_files:
        base_name = _strip_source_ext(code_fp.replace("/", ".")).split(".")[-1]
        if base_name in test_content or code_fp in failure_output:
            result[code_fp] = current_files[code_fp]
            primary_source_fp = code_fp
            break

    # If no source file matched, include the first source file mentioned in the error
    if primary_source_fp is None:
        for code_fp in current_files:
            if code_fp in failure_output:
                result[code_fp] = current_files[code_fp]
                primary_source_fp = code_fp
                break

    # Follow dependency chain to include all files the primary source depends on
    if primary_source_fp and primary_source_fp in current_files:
        if dependency_edges:
            _include_deps_from_graph(primary_source_fp, current_files, result, dependency_edges)
        else:
            _include_transitive_deps(primary_source_fp, current_files, result)

    return result


def _include_deps_from_graph(
    source_fp: str,
    current_files: dict,
    result: dict,
    dependency_edges: list,
    depth: int = 3,
) -> None:
    """
    Follow DependencyEdge graph to include exact dependencies in the repair context.

    Each DependencyEdge has source_filepath, target_filepath, and import_symbols.
    Recurses up to `depth` levels to capture the full chain (e.g. router → service → model).
    """
    if depth <= 0:
        return
    for edge in dependency_edges:
        target_fp = edge.target_filepath if hasattr(edge, 'target_filepath') else edge.get('target_filepath', '')
        edge_source = edge.source_filepath if hasattr(edge, 'source_filepath') else edge.get('source_filepath', '')
        if edge_source == source_fp and target_fp in current_files and target_fp not in result:
            result[target_fp] = current_files[target_fp]
            # Recurse into this dependency's own deps
            _include_deps_from_graph(target_fp, current_files, result, dependency_edges, depth - 1)


def _include_transitive_deps(
    source_fp: str,
    current_files: dict,
    result: dict,
    depth: int = 2,
) -> None:
    """
    Follow imports in source_fp and add any referenced current_files to result.

    Recurses up to `depth` levels so repair can see the full dependency chain
    (e.g. router → storage → models).
    """
    if depth <= 0:
        return
    content = current_files.get(source_fp, "")
    for code_fp, code_content in current_files.items():
        if code_fp in result:
            continue
        base_name = _strip_source_ext(code_fp.replace("/", ".")).split(".")[-1]
        # Check if the source file imports this module
        module_path = _strip_source_ext(code_fp.replace("/", "."))
        if base_name in content or module_path in content:
            result[code_fp] = code_content
            # Recurse into this dependency
            _include_transitive_deps(code_fp, current_files, result, depth - 1)


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
            for match in re.finditer(r'''(?:from\s+['"]|require\s*\(\s*['"])([^./'"][^'"]*?)['"]''', content):
                pkg = match.group(1)
                if pkg.startswith("@"):
                    # Scoped: @scope/name → keep full scope/name
                    parts = pkg.split("/")
                    if len(parts) >= 2:
                        packages.add(f"{parts[0]}/{parts[1]}")
                    else:
                        packages.add(pkg)
                else:
                    # Unscoped: take just the package name (before any subpath)
                    packages.add(pkg.split("/")[0])
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
