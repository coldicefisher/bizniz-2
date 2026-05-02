# Bizniz pipeline sequence

_Last updated: 2026-05-02 UTC_

## Overview

```
Problem Statement
       │
       ▼
  Planner              Step 0: Decompose into milestones (one AI call)
       │                     product-shaped slices, not engineering tasks
       │
       │  For each milestone:
       ▼
  Architect.evolve     Step 1: Decompose milestone into services
       │                     (one AI call; greenfield on M1, evolve on M2+)
       ▼
  Provisioner.evolve   Step 2: Materialize on disk + Docker images
       │                     (no AI: skeletons, infra templates, compose, env)
       ▼
  Stack Validation     Step 2.5: compose up + health check + infra debugger
       │                     (proves stack runs before engineering starts)
       ▼
  Engineer             Step 3: Per-service analyze → architecture plan → issues
       │                     (three structured AI passes)
       ▼
  Scaffold             Step 4: Deterministic stub files from architecture plan
       │
       ▼
  Phase 1 framing      Step 5: Cheap-tier coder per issue, no tests
       │
       ▼
  Preflight            Step 5.5: Static import validation + "did you mean?"
       │                     + auto-stub missing __init__.py
       ▼
  Phase 2 escalation   Step 6: Test + repair per issue per model in chain
       │                     (max_iterations=2 per model tier)
       ▼
  Phase 3 agentic      Step 7: Top-tier debugger w/ tools on remaining failures
       │                     (max_iterations=12, only if needed)
       ▼
  Image Rebuild        Step 7.5: Rebuild Docker images with final code + deps
       │                     (bakes engineer's changes into the images)
       ▼
  Integration phase    Step 8: Tests against the live Docker stack
       │                     HTTPApiTester + WebUITester + AgenticDebugger
       ▼
  Finalization         Step 9: Cost rollup, run report, milestone status
       │
       ▼
  Human verification   (pause between milestones)
       │
       └─── next milestone ───▶ back to Step 1 (evolve)
```

Cost tracking and on-disk persistence happen alongside every step — see
[cost_tracking.md](architecture/cost_tracking.md).

### Cross-language support

The pipeline is language-agnostic at the orchestration layer. Python
(FastAPI) and TypeScript (React/Angular) are first-class, with
language-specific components at each layer:

| Layer | Python | TypeScript |
|---|---|---|
| Skeleton | `bizniz-skeleton-fastapi` | `bizniz-skeleton-react`, `bizniz-skeleton-angular` |
| Test environment | `DockerPytestEnvironment` | `DockerJestEnvironment` |
| Preflight validator | `PythonPreflightValidator` (AST) | `TypeScriptPreflightValidator` |
| Scaffold | `.py` stubs + `__init__.py` | `.ts`/`.tsx` stubs |
| Coder prompt | Python-specific rules + docstrings | TypeScript-specific rules + JSDoc |
| Import tools | `search_imports`, `list_all_imports` (AST) | Future extension |

Adding a new language requires: a skeleton, a test environment, a
preflight validator, and language-specific coder/tester prompts. The
orchestration, architect, planner, and integration layers are unchanged.

### Structure — AI versus rigidity

The pipeline is designed for web applications with emphasis on SaaS.
The temptation toward fully-AI workflows is constant. The value,
however, comes from the discipline of mixing deterministic guardrails
with AI generation.

**The core principle:** deterministic guardrails make AI *more*
capable, not less. Without skeletons, the AI wastes tokens
reinventing auth. Without preflight, it wastes iterations on wrong
imports. Without stack validation, it writes code for 30 minutes
against infrastructure that doesn't run.

We rely on AI as much as possible. 100% autonomous generation is
the aspiration. But where practical, we implement deterministic
code to push AI to make faster, higher-quality leaps:

- **Skeletons** — ship working auth, routing, DB, Docker, tests.
  The AI extends, it doesn't reinvent.
- **Preflight** — catch import errors statically before burning
  test cycles. Suggest corrections with "did you mean?"
- **Scaffold** — every file exists before AI writes code.
  Eliminates "module not found" at import time.
- **Stack validation** — prove infrastructure works before
  spending on engineering. No wasted AI cost on a broken stack.
- **Image rebuild** — bake final code into images before
  integration. No stale-container debugging.
- **Integration tests as source of truth** — unit tests pass
  against mocks. Integration tests pass against reality.

The premise: AI generates, deterministic code validates and
corrects. Each guardrail exists because we measured its absence
costing real time and money in prior runs.

---

## Step 0: Planner.plan()

**File:** `bizniz/planner/planner.py`

**Input:** Problem statement (natural language), project name

**What happens:**
1. Single AI call (`planner_model`, default `gemini-pro`) with
   `JSON_SCHEMA` response.
2. AI returns a `ProjectPlan` with N milestones, each containing:
   - `name` — human-readable milestone label
   - `problem_slice` — self-contained problem statement the Architect
     can read in isolation (this is what Step 1 receives)
   - `use_cases` — user stories shipped in this milestone
   - `success_criteria` — testable outcomes
   - `depends_on_names` — topological ordering
   - `estimated_effort` — S/M/L sizing hint
3. Plan is saved to `<project>/docs/plan.json` for resume.
4. Milestones are persisted to project DB if available.

**Output:** `ProjectPlan` with ordered milestones.

**Key invariant:** Milestones are product-shaped (user value), not
engineering-shaped. The Planner says "users can register and log in"
— the Architect decides that means FusionAuth + Postgres.

**Example:** Property Manager → 5 milestones: Auth → Properties →
Tenants/Leases → Rent → Maintenance.

---

## Step 1: Architect.evolve()

**File:** `bizniz/architect/architect.py`

**Input:** Milestone's `problem_slice`, existing `SystemArchitecture`
(empty for M1, populated for M2+)

**What happens:**
1. Single AI call (`architect_model`, default `gemini-pro`) with
   `JSON_SCHEMA` response. The evolve prompt includes:
   - The milestone's problem_slice
   - All existing services (for M2+)
   - Available skeletons
   - Framework and infrastructure rules
2. AI returns services with `evolve_state` tags:
   - `"new"` — service didn't exist before
   - `"extended"` — service exists, this milestone adds to it
   - `"unchanged"` — service exists, not touched
3. Defenses: existing services preserve identity (framework, language,
   port, skeleton, image_name). Dropped services are auto-restored.
4. `ServiceDefinition` fields normalized to lowercase (service_type,
   framework, language) via Pydantic validator.
5. **FusionAuth** is mandatory whenever the problem involves user
   accounts or login. The architect prompt enforces this.

**M1 special case:** `evolve()` receives empty architecture → all
services tagged `"new"` → effectively a greenfield decompose.

**Output:** `SystemArchitecture` with evolve_state per service.

---

## Step 2: Provisioner.evolve()

**File:** `bizniz/provisioner/provisioner.py`

**Input:** Evolved `SystemArchitecture`, project name

**What happens:**
1. **Probe** — reads DB + filesystem + Docker to snapshot current state.
2. **Reconcile** per service:
   - `new` → full materialization (skeleton seed, register DB, build image)
   - `extended` → re-render templates, preserve app code, rebuild image
   - `unchanged` → re-render infra only, skip image rebuild
3. **Skeleton seeding** — clone via SSH from GitHub if not on disk,
   copy into workspace, substitute `{project_slug}` placeholders.
4. **Infrastructure templates** — postgres (init.sql, healthcheck),
   fusionauth (kickstart.json with tenant/app/roles/admin), redis.
   Lookup is case-insensitive with aliases (PostgreSQL → postgres).
5. **Compose + .env** — deterministic from template outputs.
6. **Docker image builds** for new/extended app services.

**Output:** `ProvisionResult` with per-service workspaces, image tags.

---

## Step 2.5: Stack validation

**File:** `bizniz/provisioner/stack_validator.py`

**Input:** `SystemArchitecture`, compose path, port remap

**What happens:**
1. `docker compose up -d` — bring the full stack up.
2. **Health check** each service:
   - Backend: HTTP `/openapi.json`
   - Frontend: HTTP `/`
   - Auth (FusionAuth): HTTP `/api/status`
   - Database: TCP connection
   - Cache: TCP connection
3. If unhealthy: capture container logs, dispatch `AgenticDebugger`
   against infrastructure files (Dockerfile, compose, init.sql,
   kickstart.json). Up to 3 repair iterations.
4. `docker compose down` — clean state for engineering.

**Key insight:** the stack must be proven runnable before we spend
AI cost on engineering. This step closes the gap where provisioning
wrote files and built images but never verified the stack could run.

**Output:** `StackValidation` with per-service health + logs.

---

## Step 3: Engineer.analyze() (per service)

**File:** `bizniz/engineer/engineer.py`

**Input:** Per-service problem prompt (milestone's `problem_slice`
scoped to this service), workspace with skeleton code.

**What happens — three-pass analysis:**

### Pass 1: rough draft
1. AI call → requirements, use cases, draft issues.

### Architecture planning
2. AI call → `ArchitecturePlan` (package_name, namespaces, domain
   models, modules, dependencies).

### Pass 2: refined issues with architecture context
3. Re-call with architecture plan in scope → refined issues with
   `target_files`, `test_files`, `depends_on_titles`,
   `suggested_model`, `test_setup_hint`.
4. SKELETON.md conventions are injected into both system and user
   prompts so the AI knows what files it can/can't edit.

**Only dispatched for `new` and `extended` services** — `unchanged`
services skip engineering entirely.

**Output:** `EngineeringAnalysis` (requirements, use cases, issues,
architecture plan).

---

## Step 4: Scaffold (deterministic, no AI)

**File:** `bizniz/engineer/scaffold.py`

For every namespace/domain model/module in the architecture plan,
write a stub file. Test files get pytest stubs. Every directory gets
`__init__.py`. Issues with `action == "create"` flip to `"modify"`.

This guarantees every file in the plan exists with valid imports
before any LLM does codegen. Language-agnostic — works for Python
(`.py`) and TypeScript (`.ts`/`.tsx`).

---

## Step 5: Phase 1 framing

**File:** `bizniz/engineer/framing.py`

For each issue in topological order:
1. `Coder.generate_multi(test_files=None)` — codegen only, no tests.
2. Write `FileChange`s into workspace.
3. Run preflight (Step 5.5).

Coder prompt requires **docstrings on all public functions and
classes** (Python) / **JSDoc on all exports** (TypeScript). These
feed the `search_imports` tool so downstream agents see what a
function does, not just its name.

By the time Step 6 runs tests, every issue's target files contain
real code, and later issues import working code from earlier ones.

---

## Step 5.5: Preflight validation

**File:** `bizniz/preflight/python_validator.py`

Runs after code generation, before tests:

1. **Extract imports** — AST-parse every generated `.py` file.
2. **Resolve each import** against:
   - Workspace files (is `app/api/deps.py` a real file?)
   - Declared dependencies (is `fastapi` in requirements.txt?)
   - stdlib modules
   - PyPI (HEAD check for unknown packages)
3. **"Did you mean?"** — for unresolved workspace imports, build a
   `WorkspaceIndex` of all modules + exported symbols (with full
   signatures and docstrings), fuzzy-match with
   `difflib.get_close_matches`, and append suggestions:
   ```
   Module 'app.api.deps' not found. Did you mean:
     - from app.core.auth import get_current_user, require_roles
   ```
4. **Auto-stub** — create missing `__init__.py` files.
5. **Shadow detection** — remove files that shadow packages
   (e.g. `pydantic.py` shadowing the real pydantic).
6. **Auto-install** — packages confirmed on PyPI get added to the
   install list.
7. **Hint injection** — unresolved import suggestions are stored
   and injected into the repair prompt if tests fail, so the repair
   LLM sees the correct path instead of guessing.

**Import tools available to all agents** (`bizniz/tools/import_tools.py`):
- `search_imports(symbol)` — find all modules exporting a symbol,
  with full function signatures, parameter types, and docstrings
- `list_all_imports(module)` — list every importable symbol in a
  module with signatures and types
- Available as discovery actions to coder, tester, and debugger

---

## Step 6: Phase 2 escalation chain

**File:** `bizniz/engineer/engineer.py:run_three_phase`

For each model in the escalation chain (flash → pro):
1. Fresh `CodingOrchestrator` with `max_iterations=2`.
2. `orchestrator.run_multi()` — code + tests, pytest/jest in Docker,
   one repair on failure.
3. **Regression detection** — baseline passing tests are re-checked
   after each issue. Regressions trigger repair.
4. Collection errors (pytest exit code 2/4) are classified:
   - Source import error → repair source code (with preflight hints)
   - Test import error → regenerate tests

**Execution model:** code is written on the host filesystem. Tests
execute inside fresh Docker containers (`docker run --rm`) that
volume-mount the workspace. Each test run starts a clean container —
no stale state between iterations.

---

## Step 7: Phase 3 agentic debug

For tickets still failing after Phase 2:
1. `AgenticDebugger` with full tools: `view_file`, `list_directory`,
   `search_files`, `search_imports`, `list_all_imports`,
   `run_command`, `run_tests`, `inspect_container`.
2. `inspect_container` runs commands inside the Docker container
   (where dependencies are installed).
3. `search_imports` and `list_all_imports` provide full function
   signatures with docstrings — no guessing import paths.
4. `max_iterations=12` per ticket.

---

## Step 7.5: Image rebuild

**File:** `bizniz/architect/architect.py`

After engineering completes, rebuild Docker images for all services
that passed. This bakes the engineer's final code + any new
dependencies into the images so integration tests run against the
real artifact, not the skeleton's initial state.

Uses `docker compose build <service_names>` — only rebuilds changed
services. Infrastructure images (postgres, fusionauth, redis) are
not rebuilt.

---

## Step 8: Integration phase

**File:** `bizniz/integration/runner.py`

Runs after image rebuild. Tests the services against each other in
the live Docker stack.

### 8a: Contract capture
1. Bring up backend containers.
2. Poll each backend's `/openapi.json` → save to `contracts/`.
3. Stop backends.

### 8b: Full stack up
4. `docker compose up -d` — all services including Postgres,
   FusionAuth, frontend.

### 8c: Backend integration tests
5. `HTTPApiTester` reads problem statement + OpenAPI contract →
   generates `tests/integration/test_api.py` (pytest + httpx).
6. Tests run in a **sidecar container** (`python:3.12-slim`)
   joined to the compose network. Hits `http://backend:8000`.
7. If tests fail → dispatch `AgenticDebugger`:
   - Server logs (last 60 lines) auto-prepended to error output
   - Debugger reads code, submits `code_fixes`
   - **Image rebuilt + container recreated** (`--build --force-recreate`)
   - Tests re-run in sidecar
   - Up to 3 iterations

### 8d: Frontend integration tests
8. `WebUITester` generates `.spec.cjs` Playwright tests.
9. Tests run in Playwright sidecar (`mcr.microsoft.com/playwright`)
   against `http://frontend:5173`.
10. Same debugger loop as backend on failure.

### 8e: Teardown
11. `docker compose down` (always, in finally block).

---

## Step 9: Finalization + cost rollup

1. Milestone marked `completed` or `failed` in project DB.
2. `tracker.finish_job(status)` → cost summary.
3. `write_run_report()` → per-run efficiency doc at
   `<project>/docs/runs/<job_id>.md`.
4. Plan status updated and saved to `plan.json`.
5. Human verification gate between milestones.

---

## Config reference (bizniz.yaml)

| Key | Default | Purpose |
|-----|---------|---------|
| `default_model` | `gemini-flash-lite` | Phase 1 framing tier |
| `engineer_model` | `gemini-flash` | Engineer's analyze + plan calls |
| `architect_model` | `gemini-pro` | Architect.decompose / evolve |
| `planner_model` | `gemini-pro` | Planner (one call per project) |
| `coder_models` | `[flash-lite, flash, pro]` | Escalation chain |
| `tester_models` | same | Test generation escalation |
| `repair_models` | `[flash, pro]` | Stall-escalation chain |
| `debugger_model` | `gemini-pro` | Phase 3 + integration debugger |
| `debugger_max_iterations` | 12 | Per-ticket cap in Phase 3 |
| `stall_threshold` | 3 | Consecutive failures before stall |
| `agentic_debug_threshold` | 2 | Legacy; Phase 3 forces on |
| `enable_agentic_debug` | false | Phase 3 forces on regardless |
| `max_iterations` | 20 | Hard cap per orchestrator dispatch |
| `layered_generation` | true | Always-on |
| `parallel_services` | true | Multi-service parallel dispatch |
| `max_service_workers` | 4 | Thread pool size |
