# Data Flow

This page traces the typed data that moves between agents. Every transform is a Pydantic model defined in `bizniz/<module>/types.py` (with the schemas the LLM must produce listed in [reference/schemas.md](../reference/schemas.md)).

## End-to-end shape

```
str (problem statement)
   │
   ▼  Architect.decompose
SystemArchitecture
   ├── project_name, project_slug, description
   ├── docker_compose: str (compose file as YAML text)
   └── services: List[ServiceDefinition]
        ├── name, service_type, framework, language
        ├── workspace_name, port, depends_on, requirements
        └── skeleton: Optional[str]   # picked from skeleton registry
   │
   ▼  Architect.build (per application service)
ServiceResult
   ├── service_name, workspace_name
   ├── success, issues_total, issues_passed
   └── error: Optional[str]
   │
   ▼  Engineer.analyze (one engineer per service)
EngineeringAnalysis
   ├── problem_id: int
   ├── requirements: List[EngineeringRequirement]
   ├── use_cases: List[EngineeringUseCase]
   ├── issues: List[EngineeringIssue]
   │     ├── title, description, depends_on_titles
   │     ├── target_files: List[TargetFile]      (filepath, action)
   │     ├── test_files: List[str]
   │     ├── suggested_model, test_setup_hint
   │     └── (after sort) depends_on_issues: List[int]  # db_ids
   └── architecture: ArchitecturePlan
         ├── package_name, root_namespace
         ├── namespaces, domain_models, modules, dependencies
   │
   ▼  Engineer.run_layered (one issue or one layer at a time)
List[OrchestratorResult]
   │
   ▼  CodingOrchestrator.run_multi (per issue / per layer)
OrchestratorResult
   ├── success: bool, iterations: int
   ├── changes: List[FileChange]    (filepath, code, action)
   ├── test_files: List[GeneratedTestFile]   (filepath, tests)
   ├── strategy_used: "tdd" | "code_first"
   ├── failure_context: Optional[str]
   └── architecture_drift_detected, drift_files
```

## Persistence touchpoints

Both directions of the flow above also write to durable state:

| Step | Writes to |
|------|-----------|
| `Architect.decompose` | `architecture_snapshots` (project DB) + `docs/architecture.md` |
| `Architect.build` (per service) | `services`, `build_log` (project DB) |
| `Engineer.analyze` | `problems`, `requirements`, `use_cases`, `issues`, `architecture_plans`, `architecture_namespaces`, `architecture_domain_models`, `architecture_modules`, `architecture_dependencies` (workspace DB) |
| Orchestrator iterations | `test_results`, `environment_packages` (workspace DB) |
| Issue close/reopen | `issues.status` (workspace DB) + `issue_log` (project DB) |

See [modules/db.md](../modules/db.md) for the schema and [modules/workspace.md](../modules/workspace.md) for the lazy-DB-on-workspace pattern.

## Cross-issue learning

`Engineer.run` and `run_layered` accumulate `workspace_context: dict[filepath, content]` from successfully closed issues and pass it as `workspace_context` to the next issue. The orchestrator forwards this through to `Coder.repair_multi_inline` so prior code is visible as `READ-ONLY` reference (it must not be modified).

## Cross-layer context

In layered mode, after a layer completes, the merged set of `result.changes` is added to `workspace_context` for the next layer. This is how foundation models (e.g. domain types created in layer 0) become importable when layer 1 generates services that depend on them.

## What an LLM call returns

Every agent uses `ResponseFormat.JSON_SCHEMA` for structured output:

| Agent | Schema | Returned model |
|-------|--------|----------------|
| `Architect.decompose` | `ArchitectSchema` | `SystemArchitecture` |
| `Engineer.analyze` | `EngineerSchema` | `EngineeringAnalysis` (without `architecture`) |
| `Engineer.plan_architecture` | `ArchitecturePlanSchema` | `ArchitecturePlan` |
| `Engineer.review_drift` | `ArchitectureGovernanceSchema` | `GovernanceDecision` |
| `Coder.generate_multi` | `CoderGenerateActionSchema` (tool loop) | `CoderProcessResult` |
| `Coder.repair_multi` / `repair_multi_inline` | `RepairPromptSchema` / `CoderRepairActionSchema` | `CoderProcessResult` |
| `Tester.generate_multi` | `TesterGenerateActionSchema` (tool loop) | `TesterResult` |
| `Tester.process_*` | `TesterSchema` | `TesterResult` |
| `QuickDebugger.diagnose` | `QuickDebuggerSchema` | `QuickDebuggerDiagnosis` |
| `AgenticDebugger.diagnose` | `AgenticDebuggerActionSchema` (tool loop) | `AgenticDiagnosis` |

The full schema bodies are documented in [reference/schemas.md](../reference/schemas.md).

## Failure data

When the orchestrator's repair loop hits a failure, the data path is:

```
ExecutionEnvironmentResult (success=False)
   │
   ▼  _build_failure_message
str (failure_output: stdout + traceback + error message, capped)
   │
   ▼  StallDetector.record_failure  →  stall_reason, repair_history
   ▼  AgenticDebugger.diagnose       →  AgenticDiagnosis (with code_fixes)
   ▼  Coder.repair_multi_inline  →  CoderProcessResult
```

If a stall is detected (see [modules/orchestrator_internals.md](../modules/orchestrator_internals.md)), `ModelProgression.escalate()` is called and the next iteration uses a stronger model.
