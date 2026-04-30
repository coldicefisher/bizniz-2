# Engineer Internals

`bizniz/engineer/`. Helpers below `Engineer` (which is documented at [agents/engineer.md](../agents/engineer.md)).

## Files

| File | Purpose |
|------|---------|
| `engineer.py` | The `Engineer` class itself |
| `dependency_graph.py` | Topo-sort issues into dependency layers |
| `scaffold.py` | Deterministic stub generation from an `ArchitecturePlan` |
| `types.py` | Pydantic types: `EngineeringIssue`, `ArchitecturePlan`, `DependencyLayer`, etc. |
| `prompts/` | All Engineer prompts and schemas |

## `dependency_graph.py`

Topological sorting via Kahn's algorithm.

| Function / class | Purpose |
|------------------|---------|
| `CyclicDependencyError(Exception)` | Raised when a cycle is found |
| `resolve_dependencies(issues) -> issues` | Walks `issue.depends_on_titles`, resolves each title to the corresponding `db_id`, and writes the result to `issue.depends_on_issues`. Mutates in place. |
| `sort_into_layers(issues) -> List[DependencyLayer]` | Wavefront topo sort. Layer 0 = no deps. Layer N = depends only on layers 0..N-1. Cycles raise. |

Issues within the same `DependencyLayer.issues` list have no inter-dependencies and the engineer can batch them into a single orchestrator call.

```python
from bizniz.engineer.dependency_graph import (
    resolve_dependencies, sort_into_layers, CyclicDependencyError,
)

resolve_dependencies(analysis.issues)
try:
    layers = sort_into_layers(analysis.issues)
except CyclicDependencyError:
    # Fall back to sequential
    pass
```

## `scaffold.py`

Deterministic — no AI calls. Runs between `analyze()` and `run_layered()` to ensure every file in the dependency graph exists with valid imports BEFORE the coder/tester touch anything. The coder then MODIFIES these stubs instead of creating from scratch, eliminating import-chain failures.

| Function | Purpose |
|----------|---------|
| `scaffold_from_plan(workspace, plan, issues, on_status_message=None) -> Dict[filepath, content]` | Creates namespace dirs + `__init__.py`, then writes stub files for every domain model, every module, and every test file referenced by issues. Flips `target_files[].action` from `"create"` to `"modify"` for files that now exist. |
| `_ensure_package_dirs(workspace, namespace_path)` | Walks `parts` of the path, mkdir + create empty `__init__.py` per level |
| `_ensure_all_init_files(workspace, import_map)` | After everything is written, ensures `__init__.py` exists in every directory containing a `.py` |
| `_write_stub(workspace, filepath, content, import_map)` | Skips files that already exist with non-empty content (preserves prior layer's work) |
| `_generate_domain_model_stub(model, plan)` | Pydantic-aware: detects `BaseModel` import edge to add `from pydantic import BaseModel` and `class X(BaseModel):` |
| `_generate_module_stub(module, plan)` | Class or module-level functions with the planned signatures and `pass` bodies |
| `_generate_test_stub(test_fp, issue, plan)` | Pytest stub with `def test_placeholder(): pass` |

## Engineering types (`engineer/types.py`)

### Architecture plan

```python
class ArchitectureNamespace(BaseModel):
    db_id: Optional[int] = None
    namespace_path: str       # "expense_tracker/models"
    purpose: str

class DomainModelField(BaseModel):
    name: str
    type_hint: str
    description: str = ""

class MethodSignature(BaseModel):
    name: str
    signature: str            # "def total(self) -> float"
    description: str = ""

class DomainModelDefinition(BaseModel):
    db_id: Optional[int] = None
    class_name: str
    filepath: str
    namespace_path: str = ""
    fields: List[DomainModelField] = []
    methods: List[MethodSignature] = []
    docstring: str = ""

class ModuleDefinition(BaseModel):
    db_id: Optional[int] = None
    filepath: str
    class_name: Optional[str] = None
    namespace_path: str = ""
    methods: List[MethodSignature] = []
    docstring: str = ""

class DependencyEdge(BaseModel):
    source_filepath: str
    target_filepath: str
    import_symbols: List[str] = []

class ArchitecturePlan(BaseModel):
    db_id: Optional[int] = None
    problem_id: int
    package_name: str
    root_namespace: str
    namespaces: List[ArchitectureNamespace] = []
    domain_models: List[DomainModelDefinition] = []
    modules: List[ModuleDefinition] = []
    dependencies: List[DependencyEdge] = []
    version: int = 1
```

### Engineering analysis

```python
class EngineeringRequirement(BaseModel):
    db_id: Optional[int] = None
    type: Literal["business", "functional", "nonfunctional"]
    text: str

class EngineeringUseCase(BaseModel):
    db_id: Optional[int] = None
    title: str
    description: str

class TargetFile(BaseModel):
    filepath: str
    action: Literal["create", "modify", "delete"]

class EngineeringIssue(BaseModel):
    db_id: Optional[int] = None
    title: str
    description: str
    target_files: List[TargetFile] = []
    test_files: List[str] = []
    depends_on_issues: List[int] = []     # resolved db_ids
    depends_on_titles: List[str] = []     # raw titles from the LLM
    suggested_model: Optional[str] = None
    test_setup_hint: Optional[str] = None

class DependencyLayer(BaseModel):
    layer_index: int
    issues: List[EngineeringIssue]

class EngineeringAnalysis(BaseModel):
    problem_id: int
    requirements: List[EngineeringRequirement] = []
    use_cases: List[EngineeringUseCase] = []
    issues: List[EngineeringIssue] = []
    architecture: Optional[ArchitecturePlan] = None
```

### Governance

```python
class DriftItem(BaseModel):
    filepath: str
    drift_type: str = "unplanned_file"
    class_name: Optional[str] = None
    reason: str

class DriftReport(BaseModel):
    items: List[DriftItem]

class GovernanceDecision(BaseModel):
    decision: Literal["approve", "reject", "modify"]
    reason: str
    plan_updates: Optional[Dict] = None
```

### Errors

```python
class EngineerError(Exception): ...
class EngineerBadAIResponseError(EngineerError): ...
```

## Prompts

`bizniz/engineer/prompts/` contains:

| File | Purpose |
|------|---------|
| `system_prompt.py` | `ENGINEER_SYSTEM_PROMPT` + `get_engineer_system_prompt(language)` |
| `analyze_prompt.py` | `ANALYZE_PROMPT_TEMPLATE` + `get_analyze_prompt(language)` |
| `plan_prompt.py` | `ARCHITECTURE_PLAN_PROMPT_TEMPLATE` + `get_architecture_plan_prompt(language)` |
| `governance_prompt.py` | `GOVERNANCE_PROMPT_TEMPLATE` for drift review |
| `retry_prompts.py` | `REPROMPT_TEMPLATE`, `SCOPE_REDUCTION_TEMPLATE` |
| `schema.py` | `EngineerSchema`, `ArchitecturePlanSchema`, `ArchitectureGovernanceSchema` (see [reference/schemas.md](../reference/schemas.md)) |

## Interactions

- **Used by:** `Engineer` (everywhere).
- **Calls into:** `BaseWorkspace.write_file/read_file`, the workspace DB, the AI client.

## Gotchas

- **`scaffold.py` doesn't overwrite non-empty files.** If a stub from a previous layer already has real code, it stays. This is how cross-layer code accumulates.
- **`_ensure_all_init_files` walks every `.py` in the import map.** That includes test files. So `tests/` ends up with an `__init__.py` whether you want one or not.
- **Pydantic detection in `_generate_domain_model_stub` is heuristic.** It looks for `BaseModel` in any `import_symbols` of an edge sourced from the model's filepath. If your model uses Pydantic but doesn't import it explicitly in the plan's dependencies, you'll get a plain class.
- **`resolve_dependencies` uses titles, not slugs.** If two issues have similar (but not identical) titles, `depends_on_titles` may not resolve. The LLM is asked to use exact titles, but mistakes happen — the engineer's `_dispatch_layer` falls back to running all issues if topo sort can't proceed.
- **`DependencyLayer.layer_index` is the topological depth.** Layer 0 = no inter-dependencies. Each subsequent layer depends on issues completed in earlier layers.
- **`scaffold_from_plan` flips actions.** Files it writes get their `action: "create"` flipped to `"modify"` in the issue's `target_files`. The coder's prompt template then says "you are MODIFYING existing stub files."
