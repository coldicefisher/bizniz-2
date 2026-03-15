# Bizniz Pipeline Sequence

## Overview

```
Problem Statement
       |
       v
  AutoArchitect          Step 1: Decompose into services
       |
       v
  Docker Build           Step 2: Build container images
       |
       v
  AutoEngineer           Step 3: Analyze & plan architecture
       |
       v
  Dependency Layers      Step 4: Sort issues into layers
       |
       v
  CodingOrchestrator     Step 5: Generate code + tests per layer
       |
       v
  Preflight              Step 6: Validate imports, auto-stub
       |
       v
  Test Loop              Step 7: Run tests, diagnose, repair
       |
       v
  Working Service        Step 8: All tests pass
```

---

## Step 1: AutoArchitect.decompose()

**File:** `bizniz/architect/auto_architect.py:109`

**Input:** Problem statement (natural language), project name

**What happens:**
1. Single AI call (architect_model, default gpt-4o) with JSON_SCHEMA response
2. AI returns a `SystemArchitecture`:
   - List of `ServiceDefinition` objects (name, type, framework, language, port, description)
   - Docker-compose YAML template
   - Environment variables
3. Creates project directory at `bizniz_projects/<project_slug>/`
4. Saves architecture snapshot to project DB

**Output:** `SystemArchitecture` with services list

**Example:** "Pet Groomer" → 2 services: backend (fastapi/python), frontend (react/typescript)

---

## Step 2: Docker Build

**File:** `bizniz/architect/auto_architect.py:191-250`

**Input:** Service definitions from Step 1

**What happens:**
1. For each service:
   - Create workspace directory at `project_root/<service_name>/`
   - Generate Dockerfile from templates (language-specific)
   - Write `requirements.txt` (Python) or `package.json` (TypeScript)
   - Register service in project DB
2. Write `docker-compose.yml` and `.env` to `project_root/infra/development/`
3. Build Docker images: `docker build -t <project>-<service>:dev`

**Output:** Running Docker images per service, workspace directories initialized

**Key detail:** Images are built sequentially. A build failure is logged but doesn't stop other services.

---

## Step 3: AutoEngineer.analyze()

**File:** `bizniz/engineer/auto_engineer.py:114-211`

**Input:** Problem statement + service context (framework, language, other services)

**What happens — two-pass analysis:**

### Pass 1: Rough draft
1. AI call with ANALYZE_PROMPT → requirements, use cases, draft issues
2. Persist draft issues to workspace DB (get db_ids)

### Architecture planning
3. AI call with PLAN_PROMPT → `ArchitecturePlan` (package_name, namespaces, domain_models, modules)
4. Persist plan to DB

### Pass 2: Refined issues
5. Clear message history
6. AI call with architecture context → refined issues with:
   - `target_files`: [{filepath, action: "create"|"modify"}]
   - `test_files`: ["tests/test_*.py"]
   - `depends_on_titles`: ["issue title this depends on"]
   - `suggested_model`: "gpt-4o-mini" | "gpt-4o" | "claude-sonnet"
   - `test_setup_hint`: optional guidance for test creation
7. **Delete draft issues** from DB (prevents draft leak)
8. Create refined issues in DB with new db_ids

### Package scaffolding
9. Create Python package structure (\_\_init\_\_.py files) based on architecture namespaces
10. Save `docs/engineering.md` to workspace

**Output:** `EngineeringAnalysis` with issues, architecture plan, requirements, use cases

---

## Step 4: Dependency Layers

**File:** `bizniz/engineer/dependency_graph.py`

**Input:** Issues with `depends_on_titles` from Step 3

**What happens:**
1. Resolve title references → db_id references
2. Topological sort into layers:
   - Layer 0: issues with no dependencies (models, storage)
   - Layer 1: issues depending on Layer 0 (routers, endpoints)
   - Layer 2+: deeper dependencies (integration, app factory)
3. Persist resolved `depends_on_issues` to DB

**Output:** `List[List[Issue]]` — layers of issues

**Example:**
```
Layer 0: [Service Model, Appointment Model, In-Memory Storage]
Layer 1: [Services Router, Appointments Router]
Layer 2: [Double-Booking Logic, App Factory]
```

**Key detail:** Issues within a layer are batched together and dispatched as one orchestrator run. Cross-layer code accumulates as context.

---

## Step 5: CodingOrchestrator — Initial Generation

**File:** `bizniz/orchestrator/coding_orchestrator.py:426+`

**Input per layer:** Problem description, target_files, test_files, architecture_context, strategy (CODE_FIRST or TDD), workspace_context (code from prior layers)

**What happens (CODE_FIRST strategy):**
1. Set starting model from issue's `suggested_model`
2. Sync environment packages from workspace DB
3. Load existing code from workspace (for "modify" actions)
4. **Autocoder.generate_multi()** — agentic tool loop:
   - LLM receives: issue description, target files, architecture context, existing code
   - LLM can use tools: `view_file`, `list_directory`, `search_files`
   - LLM returns `submit_code` action with file changes + dependencies
   - Typically 2-4 tool turns, 6 max
5. **Autotester.generate_multi()** — agentic tool loop:
   - LLM receives: issue description, generated code, test file paths
   - LLM returns `submit_tests` action with test files
6. Install any declared dependencies (pip/npm)

**What happens (TDD strategy):**
1. Steps 1-3 same
2. **Autotester first** — generate tests from spec (no source code)
3. **Autocoder second** — generate code to pass the tests

**Output:** `current_files` dict, `current_test_files` dict, installed packages

---

## Step 6: Preflight Validation

**File:** `bizniz/preflight/registry.py`, `bizniz/orchestrator/coding_orchestrator.py:2310-2436`

**Input:** Generated source files, test files, declared dependencies

**What happens — two phases:**

### Phase 1: Static validation (`_run_preflight`)
1. Get language-specific validator (Python, TypeScript, JavaScript, C#)
2. **Python validator:** `ast.parse()` every file, extract imports
   - Check against `sys.stdlib_module_names`
   - Auto-create missing `__init__.py` for packages
   - Auto-stub missing local modules (skeleton classes/functions)
   - Rewrite broken relative imports to absolute
   - Detect shadowed stdlib modules
3. Write stubs and rewrites to workspace
4. Install any packages flagged by validator

### Phase 2: Container import validation (`_validate_imports_in_container`)
1. Collect all imports from source + test files (AST parse)
2. Batch-try imports in Docker container via `docker exec python3 -c "import ..."`
3. Triage failures:
   - **auto_fixes**: path rewrites (wrong module path)
   - **pip_installs**: missing third-party packages
   - **ambiguous**: needs LLM resolution (one AI call)
4. Apply all fixes

**Output:** Updated `current_files` with import fixes, stubs created, packages installed

---

## Step 7: Test Loop with Repair

**File:** `bizniz/orchestrator/coding_orchestrator.py:562-1450+`

**Input:** Generated code + tests from Steps 5-6

**The loop (up to max_iterations, default 20):**

```
for iteration in 1..20:
    ┌─────────────────────────────────────────────┐
    │  Run pytest in Docker container              │
    │  (docker exec ... python3 -m pytest ...)     │
    └──────────────┬──────────────────────────────┘
                   │
         ┌─────────┴─────────┐
         │                   │
      PASS                 FAIL
         │                   │
    ┌────┴────┐        ┌─────┴──────────────────────────┐
    │Check for│        │ What kind of failure?           │
    │regressions│      │                                 │
    │in baseline│      ├─ Collection error (exit code 2) │
    │tests    │        │  → Is it a source import?       │
    └─────────┘        │    YES → repair source code     │
                       │    NO  → regenerate tests       │
                       │                                 │
                       ├─ Config error (exit code 4)     │
                       │  → repair source (pyproject.toml)│
                       │                                 │
                       ├─ Missing package                │
                       │  → pip install, retry           │
                       │                                 │
                       └─ Test failure (exit code 1)     │
                          → diagnosis & repair (below)   │
                          └──────────────────────────────┘
```

### Test failure diagnosis & repair:

```
StallDetector tracks consecutive failures
         │
         ├─ consecutive_failures >= 2 (agentic_debug_threshold)
         │     └─ AgenticDebugger.diagnose()
         │        ├─ Tool loop: view_file, search, run_tests, run_command
         │        └─ Returns: root_cause_category, fix_target, code_fixes
         │
         ├─ NOT stalled (< stall_threshold consecutive)
         │     └─ One-shot repair: _extract_failing_pair()
         │        ├─ Finds failing test + source + transitive deps
         │        └─ autocoder.repair_multi() with 2-6 files
         │
         └─ STALLED (>= stall_threshold consecutive)
               ├─ Escalate model (gpt-4o → claude-sonnet)
               ├─ stall_cycle == 1: Regenerate tests from spec
               ├─ stall_cycle == 2: Flip strategy (CODE_FIRST ↔ TDD)
               └─ stall_cycle >= 3: Full regeneration from scratch
```

### Model progression on stalls:
```
gpt-4o-mini → gpt-4o → claude-sonnet
(escalates each stall cycle)
```

**Output:** Either all tests pass → SUCCESS, or max_iterations exhausted → FAIL

---

## Step 8: Finalization

**File:** `bizniz/engineer/auto_engineer.py:384-424`

**Input:** OrchestratorResult per issue

**What happens:**
1. If success:
   - Close issue in DB (status: "closed")
   - Accumulate working code as context for next layer
   - Check for architecture drift (preflight changes vs plan)
2. If failure:
   - Reset issue to "open" in DB
   - Try fallback strategies: flip strategy → re-prompt → scope reduction
3. After all layers:
   - Sync workspace from container → host
   - Stop Docker environment
   - Return pass/fail counts to AutoArchitect

**Output:** `ServiceResult(issues_passed, issues_total, results_list)`

---

## Key Components Summary

| Component | Model | Role |
|-----------|-------|------|
| AutoArchitect | gpt-4o (architect_model) | Decomposes problem → services |
| AutoEngineer | gpt-4o (engineer_model) | Analyzes → issues, architecture plan |
| Autocoder | per-issue suggested_model | Generates/repairs source code (agentic, uses tools) |
| Autotester | per-issue suggested_model | Generates test files (agentic, uses tools) |
| AgenticDebugger | current escalated model | Diagnoses failures with full workspace access |
| Autodebugger | current model | Quick one-shot diagnosis (no tools) |
| Preflight | n/a (static analysis) | Validates imports, auto-stubs, rewrites |
| DockerPytestEnvironment | n/a | Runs pytest in isolated container |

---

## Config Reference (bizniz.yaml)

| Key | Default | Purpose |
|-----|---------|---------|
| `default_model` | gpt-4o-mini | Starting model for code gen |
| `engineer_model` | gpt-4o | Model for engineering analysis |
| `architect_model` | gpt-4o | Model for architecture decomposition |
| `autocoder_models` | [gpt-4o-mini, gpt-4o, claude-sonnet] | Escalation chain for code gen |
| `repair_models` | [gpt-4o, claude-sonnet] | Escalation chain for repairs |
| `stall_threshold` | 3 | Consecutive failures before declaring stall |
| `agentic_debug_threshold` | 2 | Consecutive failures before agentic debugger |
| `max_iterations` | 20 | Max test-repair cycles per orchestrator run |
| `layered_generation` | true | Sort issues by dependency layers |
| `parallel_services` | true | Dispatch services concurrently |
| `max_service_workers` | 4 | Thread pool size for parallel services |
