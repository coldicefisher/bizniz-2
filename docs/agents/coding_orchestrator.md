# CodingOrchestrator

`bizniz/orchestrator/coding_orchestrator.py`. The repair loop that turns a single issue into passing tests.

## Purpose

The orchestrator owns the iterative coding/testing/repair cycle for one engineering issue (or one batched layer of issues). It composes Autocoder + Autotester + (Quick or Agentic) Debugger inside a single loop with safeguards against stalls, regression, missing packages, and pytest collection errors.

It supports two strategies (`bizniz/orchestrator/strategy.py`):

| Strategy | Order | Repair behavior |
|----------|-------|-----------------|
| `TDD` | tests first → code | tests are the spec; only code is repaired |
| `CODE_FIRST` | code first → tests | both can be repaired |

## Constructor (selected parameters)

| Parameter | Type | Notes |
|-----------|------|-------|
| `autocoder` | `Autocoder` | required |
| `autotester` | `Autotester` | required |
| `test_environment` | `BaseExecutionEnvironment` | `DockerPytestEnvironment` or `DockerJestEnvironment` |
| `workspace` | `BaseWorkspace` | required |
| `autodebugger` | `Optional[Autodebugger]` | the QuickDebugger; `None` falls back to a heuristic repair path |
| `client` | `Optional[BaseAIClient]` | shared client reference, needed for model escalation |
| `client_factory` | `Optional[Callable[[str], BaseAIClient]]` | preferred way to swap models — creates a fresh client per model |
| `debugger_factory` | `Optional[Callable[[], AgenticDebugger]]` | factory for deep diagnosis sessions |
| `model_progression` | `Optional[ModelProgression]` | shared progression; per-agent overrides below take precedence |
| `autocoder_progression` / `autotester_progression` / `repair_progression` | `Optional[ModelProgression]` | per-agent escalation lists |
| `stall_threshold` | `int = 2` | consecutive failures before stalling |
| `agentic_debug_threshold` | `int = 2` | consecutive failures before invoking the deep debugger |
| `max_iterations` | `int = 20` | hard cap on the inner loop |
| `language` | `str = "python"` | drives `LanguageStrategy` selection |
| `enable_agentic_debug` | `bool = True` | toggles deep debugging |
| `stall_recovery` | `"full"` / `"regenerate"` / `"none"` | what to do on stall |

Class constants: `MAX_TOTAL_COLLECTION_ERRORS = 12`, `MAX_PACKAGE_INSTALL_ATTEMPTS = 3`, `WALL_CLOCK_TIMEOUT = 1800` (30 minutes).

## Public entry points

### `run(prompt, code_filename, test_filename, strategy=TDD) → OrchestratorResult`

Single-file legacy mode. Used for tiny one-file demos. Generates code + tests, runs pytest, repairs in a loop.

### `run_multi(prompt, target_files, test_files, architecture_context="", initial_model=None, strategy=TDD, workspace_context=None, dependency_edges=None, prior_test_files=None) → OrchestratorResult`

The main entry used by `AutoEngineer`. Notable parameters:

| Parameter | Purpose |
|-----------|---------|
| `target_files: List[dict]` | `[{filepath, action: "create"|"modify"}]` |
| `test_files: List[str]` | test filepaths the autotester will (re)write |
| `architecture_context: str` | formatted plan text from `AutoEngineer.format_architecture_context` |
| `initial_model` | suggested starting model (set on every progression) |
| `workspace_context: dict` | files from previously resolved issues; sent as READ-ONLY |
| `dependency_edges: list[DependencyEdge]` | exact import edges from the plan |
| `prior_test_files: Optional[Set[str]]` | regression baseline scope; only these files are checked for regressions |

Top-level steps inside `run_multi`:

1. **Scope architecture context** to only this issue's files + their plan dependencies (`_scope_architecture_context`).
2. **Snap to suggested model.** If `initial_model` is provided, every progression's `set_start(...)` is called and a fresh client is built (via `client_factory` if available; otherwise `client.set_model(...)` in place).
3. **Sync env packages.** Install any pip packages remembered in the workspace DB.
4. **Snapshot baseline.** Capture passing tests before we start, scoped to `prior_test_files` if provided.
5. **Build extra prompt context.** Cross-issue code, workspace manifest, import map, installed packages, stub-file warnings — all appended to `extra_context`.
6. **Generate.** Strategy-dispatched: TDD does tests-then-code, CODE_FIRST does code-then-tests.
7. **Loop.** Run pytest; on failure detect missing packages, classify collection errors, then either invoke the autodebugger / agentic debugger or fall through to a heuristic repair.
8. **Stall detection.** `StallDetector` watches code-hash repeats, error signatures, and consecutive failures. On stall: escalate the model (via `ModelProgression`) and reset counters.
9. **Wall-clock and max-iterations exits.** `OrchestratorMaxIterationsError` if the loop runs out.

Returns `OrchestratorResult(success, changes, test_files, iterations, strategy_used, failure_context, architecture_drift_detected, drift_files)`.

## Major helpers (private)

| Helper | What it does |
|--------|--------------|
| `_apply_language_system_prompts()` | Overrides autocoder/autotester system prompts via `LanguageStrategy` for non-Python projects |
| `_scope_architecture_context(arch_context, target_files)` | Trims the plan summary to just files in this issue + transitive dependencies |
| `_sync_environment_packages(log)` | Reads `environment_packages` from workspace DB and installs each one in the test environment |
| `_proactive_package_install(code_dict, test_dict, log)` | Scans imports in newly-generated files and pip-installs any missing third-party package |
| `_install_project_editable(log)` | One-shot `pip install -e .` if a `pyproject.toml` exists; sets `_editable_install_failed` on first failure to avoid repeating |
| `_get_passing_tests(log, restrict_to=None)` | Runs each test file individually to record which were passing; used as the regression baseline |
| `_build_workspace_manifest()` | Compact "filepath → exported names" summary of every existing source file |
| `_build_import_map()` | "from X import Y" lines the LLM is told to use verbatim |
| `_get_installed_packages()` | Shells into the runner container and lists installed packages |
| `_is_stub_file(path, content)` | Heuristic: file is a stub if it's only docstring + `pass` / `...` / `NotImplemented` |
| `_handle_failure_with_debugger(...)` | Calls the `Autodebugger.diagnose(...)` once per iteration; merges its `relevant_files` into the repair payload, then calls `Autocoder.repair_multi_inline(...)`. Escalates to `AgenticDebugger` after `agentic_debug_threshold` consecutive failures. |
| `_handle_failure_heuristic(...)` | Same shape as above but skips the debugger; used when `autodebugger` is None |

## Strategy + escalation

```python
from bizniz.orchestrator.strategy import CodingStrategy

orchestrator.run_multi(
    prompt=issue.description,
    target_files=[{"filepath": "calc.py", "action": "create"}],
    test_files=["tests/test_calc.py"],
    strategy=CodingStrategy.CODE_FIRST,
    initial_model="gpt-4o-mini",
)
```

Escalation is driven by `StallDetector` (see [modules/orchestrator_internals.md](../modules/orchestrator_internals.md)). When `is_stalled` is True, the orchestrator calls `ModelProgression.escalate()` and switches the client (or calls `client.set_model(new)`), then re-runs the loop with reset stall counters but preserved `repair_history` for context.

## Interactions

- **Calls into:** `Autocoder.{generate_only, generate_multi, repair, repair_multi, repair_multi_inline}`, `Autotester.{process_from_prompt, generate_multi}`, `Autodebugger.diagnose`, `AgenticDebugger.diagnose`, `BaseExecutionEnvironment.execute`, `StallDetector`, `ModelProgression`, the language strategy (`LanguageStrategy`), `bizniz.preflight.registry.get_validator`.
- **Called by:** `AutoEngineer._run_orchestrator` (one orchestrator per attempt — a fresh instance for each retry).

## Gotchas

- **`run_multi` is the real entry point.** `run` is single-file legacy. The engineer always uses `run_multi`.
- **`initial_model` must be in the progression list.** `set_start(name)` silently does nothing if the name isn't present, so a stall would still escalate from the (cheaper) default starting position.
- **`client_factory` is preferred over `client.set_model(...)`.** Some clients accumulate state per-model; the factory approach guarantees a clean client. If the factory is None, the orchestrator falls back to mutating the shared client.
- **Drift detection runs in the orchestrator, governance runs in the engineer.** The orchestrator only sets `architecture_drift_detected` and `drift_files`; `AutoEngineer._finalize_dispatch` is what actually calls `review_drift(...)`.
- **Layered batching shares one orchestrator call.** When multiple issues are in the same dependency layer, `AutoEngineer._dispatch_layer` merges them into one `run_multi` invocation. Failures roll up to the layer, not individual issues — diagnose accordingly.
- **Test runs install packages on the fly.** `_proactive_package_install` and the missing-package detector each shell into the running container with `pip install`. The container persists across iterations (see [modules/environment.md](../modules/environment.md)), so the install survives.
- **Stall counters reset after escalation, but `repair_history` does not.** This is on purpose — the deep diagnosis path uses the history as evidence.
- **Wall-clock timeout is 30 minutes per `run_multi` call.** The cap is hardcoded as `WALL_CLOCK_TIMEOUT`. Long, slow models (Opus on a giant repo) can hit this.
