# AutoEngineer

`bizniz/engineer/auto_engineer.py`. The per-service software-engineering layer between the architect and the orchestrator.

## Purpose

Given a service-scoped problem statement, the engineer:

1. Decomposes the statement into business / functional / non-functional requirements, use cases, and discrete coding **issues**.
2. Plans an architecture (`ArchitecturePlan`): package name, namespaces, domain models, modules, and the import dependency graph.
3. Re-runs the analysis with the plan as context so the issues reference real files.
4. Scaffolds stub files for every planned module/test (no AI involved — see `engineer/scaffold.py`).
5. Sorts issues into dependency layers (`engineer/dependency_graph.py`) and dispatches a `CodingOrchestrator` per layer / issue.
6. Manages multi-attempt retry strategies (CODE_FIRST → TDD → re-prompt → scope reduction) when an issue fails.
7. Reviews architectural drift via a governance LLM call.

It persists everything to the **workspace DB** (`WorkspaceDB` / `WorkspaceScope`), one DB per service.

## Constructor

| Parameter | Type | Notes |
|-----------|------|-------|
| `client` | `BaseAIClient` | LLM for analysis, planning, governance, re-prompts |
| `environment` | `BaseExecutionEnvironment` | Forwarded to the orchestrator |
| `workspace` | `BaseWorkspace` | Service-scoped workspace (one engineer ↔ one workspace) |
| `orchestrator_factory` | `Callable[..., CodingOrchestrator]` | Zero-arg-ish factory; called per-issue with optional `suggested_model=...` |
| `max_retries` | `int = 3` | AI-call retry budget |
| `language` | `str = "python"` | Drives prompt selection (`get_engineer_system_prompt`, `get_analyze_prompt`, `get_architecture_plan_prompt`) |
| `available_models` | `Optional[List[str]]` | Models the LLM can pick from when annotating `suggested_model` per issue |
| `on_event`, `on_status_message` | callbacks | Standard agent callbacks |

## Public API

### `analyze(problem_statement) → EngineeringAnalysis`

The full analysis sequence (see `engineer/auto_engineer.py:114-220`):

1. Save problem statement → `problem_id`.
2. AI call with `AutoEngineerSchema` → requirements, use cases, draft issues.
3. AI call with `ArchitecturePlanSchema` → `ArchitecturePlan`.
4. Re-analyze with the plan as context. Delete draft issues and persist the refined ones.
5. Backfill `test_setup_hint` for any issue that touches an endpoint/route/middleware but had an empty hint (`_backfill_test_setup_hints`).
6. For Python: create the package structure (`workspace.init_as_package`).
7. Run `scaffold_from_plan(...)` to write stub source/test files.
8. Save human-readable `docs/engineering.md`.

### `dispatch(issue_id, workspace_context=None) → OrchestratorResult`

Runs one issue through the orchestrator with the multi-attempt retry chain:

| Attempt | Strategy | What changes |
|---------|----------|--------------|
| 1 | CODE_FIRST | default — generates code first, then tests |
| 2 | TDD | tests first, code generated to satisfy them |
| 3 | re-prompt | rewrites the issue description with failure context (LLM call) |
| 4 | scope reduction | simplifies issue to its minimum viable form |

Failure context is only available between attempts because `OrchestratorResult.failure_context` carries the last failure output. The first success short-circuits the chain.

### `run(problem_statement) → list[OrchestratorResult]`

Sequential dispatch of every issue produced by `analyze`. Cross-issue learning: working code from passed issues is accumulated into `workspace_context` and passed to subsequent issues.

### `run_layered(problem_statement, analysis=None) → list[OrchestratorResult]`

Layered dispatch:

1. Resolve `depends_on_titles` → `depends_on_issues` (db_ids) and persist.
2. Topologically sort with `sort_into_layers(...)`. On `CyclicDependencyError`, fall back to sequential `run(...)`.
3. For each layer: if it has 1 issue, call `dispatch(...)`; otherwise batch all issues in the layer into one orchestrator call via `_dispatch_layer(...)`.
4. After each layer, accumulate working code into `workspace_context` for the next layer.

### `plan_architecture(problem_id, analysis) → ArchitecturePlan`

Standalone access to step 2 of `analyze`. Calls AI with `ArchitecturePlanSchema`, persists to the workspace DB, returns the populated plan.

### `create_package_structure(plan)`

Calls `workspace.init_as_package(plan.package_name, ...)`. **Only the root package directory and `pyproject.toml` are created** — sub-namespaces are NOT pre-created because the autocoder may produce single-file modules (`models.py`) instead of packages (`models/__init__.py`), and having both causes import collisions.

### `review_drift(plan, drift_items) → GovernanceDecision`

Calls AI with `ArchitectureGovernanceSchema`. Returns `approve` / `reject` / `modify`. If `modify`, the plan is updated in the DB.

### `format_architecture_context(plan) → str`

Static-style formatter that produces a compact summary string of the plan, used as `architecture_context` in orchestrator prompts.

### Lifecycle

- `close()` closes the workspace DB.
- Implements `__enter__` / `__exit__` so it can be used as a context manager (which is what `AutoArchitect`'s `engineer_factory` produces).

## Retry strategies

The retry chain in `dispatch(...)` uses these private helpers:

| Helper | Output |
|--------|--------|
| `_run_orchestrator(row, target_files, test_files, arch_context, suggested_model, strategy, workspace_context, log, prompt_override=None, dependency_edges=None)` | Builds the prompt (appending `test_setup_hint` if available), calls `orchestrator.run_multi(...)`, catches `OrchestratorMaxIterationsError` and `AIInsufficientFunds` (re-raised). Returns an `OrchestratorResult`. |
| `_reprompt_issue(row, result, log)` | One LLM call with `REPROMPT_TEMPLATE` to rewrite the issue description |
| `_reduce_scope(row, result, log)` | One LLM call with `SCOPE_REDUCTION_TEMPLATE` to simplify the issue |
| `_finalize_dispatch(issue_id, result, log)` | Runs governance review on detected drift; closes or reopens the issue |
| `_backfill_test_setup_hints(issues, plan, log)` | Auto-generates a TestClient hint for integration-style issues that lack one |
| `_dispatch_layer(layer, analysis, workspace_context)` | Merges all issues in a layer into one combined prompt + target files + test files; runs once with `CodingStrategy.CODE_FIRST`. |

## Example

```python
from bizniz.engineer.auto_engineer import AutoEngineer
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator

def make_orchestrator(suggested_model=None) -> CodingOrchestrator:
    return CodingOrchestrator(
        autocoder=...,
        autotester=...,
        autodebugger=...,
        test_environment=...,
        workspace=workspace,
        client=client,
        ...
    )

with AutoEngineer(
    client=client,
    environment=env,
    workspace=workspace,
    orchestrator_factory=make_orchestrator,
    language="python",
    available_models=["gpt-4o-mini", "gpt-4o", "gpt-5"],
    on_status_message=print,
) as engineer:
    results = engineer.run_layered("Build a roman numeral converter library")
    for r in results:
        print(r.success, r.iterations, r.strategy_used)
```

## Interactions

- **Calls into:** `BaseAIClient.get_text` (analysis, plan, governance, re-prompt, scope reduction), `WorkspaceDB.save_problem / save_issue / save_architecture_plan / ...`, `scaffold_from_plan`, `resolve_dependencies` + `sort_into_layers`, `orchestrator_factory` then `orchestrator.run_multi`, `workspace.init_as_package`.
- **Called by:** `AutoArchitect._dispatch_engineer` (one engineer per service).

## Gotchas

- **`_process_system_prompt` reads `self._language`.** That's why `self._language` is set BEFORE `super().__init__(...)`.
- **The first analyze call's draft issues are deleted.** `analyze` runs analysis twice (with and without architecture context). The first call's issues are wiped from the DB before the second call's are saved (`workspace.db.delete_issues(problem_id)`). Don't rely on the draft IDs.
- **Sub-namespaces are NOT auto-created.** Only the root package dir + tests dir + pyproject.toml. Subdirs are created lazily by the autocoder and the scaffold step.
- **`run_layered` falls back to `run` on cycles.** `CyclicDependencyError` is caught and silently downgrades to sequential dispatch.
- **Governance only runs on dispatch finalization.** If `result.architecture_drift_detected` is True and `result.drift_files` is non-empty, `review_drift` runs and may modify the plan in the DB.
- **`run_multi` is the orchestrator entry point used here**, not `run`. The single-file `run` is reserved for older single-file cases.
- **Package install hint dance:** `_backfill_test_setup_hints` looks for a "create_app", "factory", or class-named "App" in the plan's modules to derive a FastAPI/Express test-client recipe. If your plan doesn't have one, integration issues stay unhinted.
