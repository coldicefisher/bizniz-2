# Bizniz pipeline sequence

_Last updated: 2026-04-30 UTC_

## Overview

```
Problem Statement
       │
       ▼
  Architect          Step 1: Decompose into services (one AI call)
       │
       ▼
  Provisioner            Step 2: Materialize plan on disk + Docker images
       │                  (no AI: skeletons, infra templates, compose, env)
       ▼
  Engineer           Step 3: Per-service analyze → architecture plan → issues
       │                  (three structured AI passes)
       ▼
  Scaffold               Step 4: Deterministic stub files from architecture plan
       │
       ▼
  Phase 1 framing        Step 5: Cheap-tier coder per issue, no tests
       │
       ▼
  Phase 2 escalation     Step 6: One attempt per issue per model in chain,
       │                  with tests + repair (max_iterations=2)
       ▼
  Phase 3 agentic        Step 7: Top-tier debugger w/ tools on remaining failures
       │                  (max_iterations=12, only if needed)
       ▼
  Working Service        Step 8: All tests pass; cost rolled up to jobs table
```

Cost tracking and on-disk persistence happen alongside every step — see
[cost_tracking.md](architecture/cost_tracking.md).

---

## Step 1: Architect.decompose()

**File:** `bizniz/architect/architect.py`

**Input:** Problem statement (natural language), project name

**What happens:**
1. Single AI call (`architect_model`, default `gemini-flash`) with
   `JSON_SCHEMA` response.
2. AI returns a `SystemArchitecture`:
   - `services: List[ServiceDefinition]` — each with name, type
     (`backend`/`frontend`/`worker`/`database`/`cache`/`proxy`/`auth`),
     framework, language, port, depends_on, requirements, and a
     `skeleton` choice (one of the 6 registered skeletons or `"none"`).
   - `description` and `project_slug`.
3. Cost tracker opens a `job_id` and tags the call with
   `phase=architect.decompose`. Records buffer in memory until the
   project DB exists (Step 2).

**Output:** `SystemArchitecture` with services list. **No file writes,
no docker subprocess.** The architect is pure planning.

**Example:** "Pet Groomer with login" → 4 services: backend (fastapi),
frontend (react), auth (fusionauth), postgres (database).

---

## Step 2: Provisioner.provision()

**File:** `bizniz/provisioner/provisioner.py`

**Input:** `SystemArchitecture` from Step 1, project name

**What happens:**
1. **Free-port allocation** — walks every host port and bumps any that
   collide with each other or with something already bound on the dev
   machine.
2. **Project structure** — creates `project_root/` and
   `project_root/infra/development/`. Saves an architecture snapshot
   to `ProjectDB`.
3. **Cleanup** — removes any leftover `<project_slug>-*` Docker images
   and dangling containers from prior builds.
4. **Per-service materialization:**
   - **Infrastructure services** (database, cache, proxy, auth) →
     render the matching template:
     - `postgres` → compose entry with healthcheck + `pgdata` volume,
       `init.sql` that creates the FusionAuth DB alongside the app DB.
     - `redis` → compose entry with healthcheck.
     - `fusionauth` → compose entry depending on postgres-healthy +
       full `kickstart.json` (default tenant issuer, application named
       after project_slug, admin/user roles, OAuth redirects for both
       React and Angular, JWT settings, initial admin user, bootstrap
       API key).
   - **Application services with a skeleton** → seed from
     `~/bizniz-skeleton-<name>` (skipping `.git`, `node_modules`,
     lockfiles), substitute `{project_slug}` placeholders, mirror the
     skeleton's Dockerfile into `infra/development/<svc>/Dockerfile`.
   - **Application services without a skeleton** → render the generic
     `PythonAppTemplate` or `TypeScriptAppTemplate`: Dockerfile +
     requirements.txt or package.json with framework defaults.
5. **Compose + .env:** `compose_builder.build_compose()` assembles a
   single YAML deterministically from the per-service template outputs
   (no AI parsing). `env_builder.build_env_file()` aggregates env vars
   contributed by every template, grouped by prefix.
6. **Docker image builds** for app services (`build_images=True` by
   default; pass `False` for tests).
7. Cost tracker attaches to the project DB; buffered records flush.

**Output:** `ProvisionResult` with project_root, compose_path,
env_path, per-service workspaces, image tags, port_remap dict.

See [architect_provisioner_split.md](architecture/architect_provisioner_split.md).

---

## Step 3: Engineer.analyze() (per service)

**File:** `bizniz/engineer/engineer.py`

**Input:** Per-service problem prompt, service framework + language,
existing architecture snapshot from `ProjectDB`.

**What happens — three-pass analysis:**

### Pass 1: rough draft
1. AI call with `ANALYZE_PROMPT` → requirements, use cases, draft issues.
2. Persist draft issues to workspace DB.

### Architecture planning
3. AI call with `PLAN_PROMPT` → `ArchitecturePlan` (package_name,
   namespaces, domain models, modules, dependencies).

### Pass 2: refined issues with architecture context
4. Clear message history, re-call analyze with the architecture plan
   in scope → refined issues with:
   - `target_files: [{filepath, action: "create"|"modify"}]`
   - `test_files: ["tests/test_*.py"]`
   - `depends_on_titles`
   - `suggested_model`
   - `test_setup_hint`
5. Delete draft issues, write refined ones.

Cost tracker tags each call with
`(service_name, phase=engineer.analyze)`.

**Output:** `EngineeringAnalysis` (requirements, use cases, issues,
architecture plan).

---

## Step 4: Scaffold (deterministic, no AI)

**File:** `bizniz/engineer/scaffold.py`

For every namespace / domain model / module in the architecture plan,
write a stub file (`class Foo: pass`, function signatures). Test files
get pytest stubs. Every directory containing a `.py` file gets an
`__init__.py`. Issues with `target_files[].action == "create"` get
flipped to `"modify"` since the stubs now exist.

This guarantees every file in the plan exists with valid imports
before any LLM does codegen — eliminates a class of "module not found"
failures.

---

## Step 5: Phase 1 framing (`frame_issues`)

**File:** `bizniz/engineer/framing.py`

**Input:** Issues in topological order, cheap coder

For each issue:
1. Run `Coder.generate_multi(test_files=None)` — codegen only, no
   tests, no Docker.
2. Write the generated `FileChange`s into the workspace.
3. Run preflight (auto-stub missing locals, rewrite broken imports).
4. Cost tracker tags with `(issue_id=N, phase=phase1.frame)`.

By the time Step 6 runs tests, every issue's target files contain
real working code — not empty stubs — and later issues import working
code from earlier ones.

---

## Step 6: Phase 2 escalation chain

**File:** `bizniz/engineer/engineer.py:run_three_phase`

For each model in `coder_models[1:]` (the escalation chain after
the framing tier — typically `gemini-flash`, then `gemini-pro`):

1. Iterate every still-failing issue in topological order.
2. Set cost-tracker context: `phase=phase2.<model>, issue_id=N`.
3. Build a fresh `CodingOrchestrator` with `max_iterations=2` and
   `enable_agentic_debug=False`.
4. `orchestrator.run_multi()` — generates code + tests, runs pytest /
   jest in Docker, attempts one repair on failure.
5. If success → close issue in DB; if failure → ticket carries to next
   model.

The collection-error router (`_is_source_import_error`) chooses
between source repair and test regen on each pytest exit-code-2
failure. Universal config files (jest.config, package.json,
pyproject.toml, Dockerfile, etc.) are auto-loaded into the writable
repair pool — see [error_classification.md](architecture/error_classification.md).

---

## Step 7: Phase 3 agentic debug (only if needed)

For any tickets still failing after Phase 2:

1. Set cost-tracker context: `phase=phase3.agentic`.
2. Build a `CodingOrchestrator` on `debugger_model` (default
   `gemini-pro`) with `enable_agentic_debug=True` and
   `max_iterations=debugger_max_iterations` (default 12).
3. The agentic debugger has full tools: `view_file`, `list_directory`,
   `search_files`, `run_command`, `run_tests`. It can read the
   workspace, diagnose the failure, and patch source/tests/configs
   directly.

In the 2026-04-29 baseline run, Phase 3 was never triggered —
Phase 1+2 resolved 8/8 tickets.

---

## Step 8: Finalization + cost rollup

1. Each closed issue is marked `closed` in the workspace DB.
2. `architect.build()` calls `tracker.finish_job(status)` —
   refreshes the job row with totals (calls, tokens, cost).
3. The build prints a `CostSummary.format()` to stdout:

   ```
   calls=42  input=128,440  output=53,920  total=$0.1832
     by model:
       gemini-2.5-flash-lite               calls=38  $0.0182
       gemini-3.1-flash-lite-preview       calls= 3  $0.0102
       gemini-3.1-pro-preview              calls= 1  $0.1338
     by agent:
       coder         calls=22  $0.1402
       tester        calls=12  $0.0238
       engineer     calls= 7  $0.0183
       architect    calls= 1  $0.0009
   ```

4. Failed runs still record cost (so you can see what a failed run
   cost) and finish with `status=failed`.

---

## Step 9: Per-run efficiency log

After a successful run, copy the format from
`docs/runs/2026-04-29_pet_groomer_three_phase_baseline.md` into a new
`docs/runs/<date>_<project>_<note>.md`. Capture architecture, models
config, per-issue table (which phase + model solved each), wall-clock
time, total cost, and a "compare with prior runs" row. The series of
run docs is the efficiency log.

Cost rollups for the run are available via:

```python
from bizniz.project.project import Project
project = Project(root="/home/jamey/bizniz_projects/<slug>", project_name="X")
for row in project.db.cost_by_issue(job_id=...):
    print(row["issue_id"], row["calls"], row["total_cost"])
```

---

## Config reference (bizniz.yaml)

| Key | Default | Purpose |
|-----|---------|---------|
| `default_model` | `gemini-flash-lite` | Phase 1 framing tier |
| `engineer_model` | `gemini-flash` | Engineer's analyze + plan calls |
| `architect_model` | `gemini-flash` | Architect.decompose |
| `coder_models` | `[gemini-flash-lite, gemini-flash, gemini-pro]` | Escalation chain (Phase 1 = first; Phase 2 = the rest) |
| `tester_models` | same | Test generation escalation |
| `repair_models` | `[gemini-flash, gemini-pro]` | Stall-escalation chain |
| `debugger_model` | `gemini-pro` | Phase 3 agentic debugger |
| `debugger_max_iterations` | 12 | Per-ticket cap in Phase 3 |
| `stall_threshold` | 3 | Consecutive failures before declaring stall |
| `agentic_debug_threshold` | 2 | When agentic debug kicks in (legacy; auto-on in Phase 3) |
| `enable_agentic_debug` | false | Phase 3 forces this on regardless |
| `max_iterations` | 20 | Hard cap inside any single orchestrator dispatch |
| `layered_generation` | true | Currently always-on |
| `parallel_services` | true | Multi-service projects dispatch in parallel |
| `max_service_workers` | 4 | Thread pool size |
