# Bizniz Auto-Engineering System Architecture

_Last updated: 2026-04-30 UTC_

## Overview

The **Bizniz** auto-engineering system takes a natural-language problem
statement and produces a working multi-service application: planning,
infrastructure provisioning, code generation, automated testing, and
iterative repair — all driven by AI agents.

The pipeline has five levels of agents and one materializer:

| Component | Role | Has AI? |
|---|---|---|
| `Planner` | Decompose project into ordered milestones (user value) | Yes (one call) |
| `AutoArchitect` | Decompose problem (or milestone) into services + dependencies + ports | Yes (one call) |
| `Provisioner` | Materialize the plan: directory tree, skeletons, infra templates, compose, .env, Docker images | No |
| `AutoEngineer` | Per service: produce issues, architecture plan, dispatch | Yes (multi-pass analysis) |
| `CodingOrchestrator` | Per issue: codegen + tests + repair loop | Yes (per-iteration) |
| `Autocoder` / `Autotester` / `AgenticDebugger` | Specialized sub-agents the orchestrator dispatches | Yes |

A `CostTracker` records every AI call to the project SQLite DB so cross-run
analysis (per milestone, per issue, per service, per model, per phase) is
just a SQL query.

> **Status**: the Planner exists as of 2026-04-30 but is not yet wired
> into `architect.build()`. The current build loop decomposes from the
> full problem statement directly. Multi-week evolve-mode (Planner →
> per-milestone Architect.evolve → Provisioner.evolve) is the next
> branch. See [planner.md](architecture/planner.md).

---

## High-level architecture

```
                  problem statement
                          │
                          ▼
                ┌────────────────────┐
                │      Planner       │   sequence user value — one AI call
                │      .plan()       │   → ProjectPlan with N milestones
                └─────────┬──────────┘   (not yet wired into build loop)
                          │ (future: per milestone)
                          ▼
                ┌────────────────────┐
                │    AutoArchitect   │   plan services — one AI call
                │   .decompose()     │   → SystemArchitecture
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │    Provisioner     │   no AI — materialize plan
                │   .provision()     │   → directory tree, skeletons,
                │                    │      infra templates (postgres,
                │                    │      redis, fusionauth), compose,
                │                    │      .env, Docker images
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │    AutoEngineer    │   per service
                │ .run_three_phase() │   Phase 1 frame → Phase 2 escalate
                │                    │   → Phase 3 agentic debug
                └─────────┬──────────┘
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
  ┌──────────┐  ┌────────────┐  ┌────────────────┐
  │ Autocoder│  │ Autotester │  │ AgenticDebugger│
  │  + tools │  │            │  │ + run_command  │
  │          │  │            │  │ + run_tests    │
  └──────────┘  └────────────┘  └────────────────┘
                          │
                          ▼
                ┌────────────────────┐
                │  Test Loop         │   pytest / jest in Docker
                │  + repair          │   + collection-error routing
                │  + regression check│   + config-aware repair
                └────────────────────┘
```

---

## Core components

### `BaseAIAgent` (`bizniz/core/agent.py`)

Foundation class for AI-driven agents. Standardizes:
- AI client wiring (OpenAI, Claude, Gemini)
- Execution environment access
- Workspace I/O
- Message-history management with system-prompt override
- Tagging the client with `_caller_agent` so cost tracking knows which
  agent made each AI call

### `Planner` (`bizniz/planner/`)

**Sequencing user value.** One AI call (`plan`) returns a
`ProjectPlan` — an ordered list of `Milestone`s, each with use cases,
success criteria, depends_on_names, and a self-contained
`problem_slice` the Architect can later decompose standalone.

The Planner does NOT decide services, frameworks, ports, or file
structure — those are the Architect's concerns. The Planner reasons
in product terms: "what does the user get first? what depends on
what? how do I cut this into 1-2 week deliverables?"

Top-tier model (default `gemini-pro`) — one call per project, the
quality bump is foundational. Persists to `project_plans` +
`milestones` tables in `ProjectDB`. See [planner.md](architecture/planner.md).

### `AutoArchitect` (`bizniz/architect/auto_architect.py`)

**Pure planning.** One AI call (`decompose`) returns a `SystemArchitecture`
listing services with name, type, framework, language, port, depends_on,
and skeleton choice. Orchestrates the rest of the pipeline (provision →
dispatch engineers) but does not write files or build images directly.

The architect's prompt instructs the LLM to:
- Pick from registered skeletons (fastapi / react / angular / teams-*)
  for any application service.
- Add a `fusionauth` auth service AND `postgres` database whenever the
  project has user accounts.
- Emit only the structured plan; the Provisioner generates compose
  deterministically.

See [architect_provisioner_split.md](architecture/architect_provisioner_split.md).

### `Provisioner` (`bizniz/provisioner/`)

**Pure materialization.** Takes a `SystemArchitecture` and produces:
- Project directory tree at `project_root/`
- Per-service workspaces under `project_root/<workspace_name>/`
- Skeleton seeding from `~/bizniz-skeleton-*` for app services
- Infrastructure templates: postgres (with `init.sql`), redis,
  fusionauth (with full `kickstart.json` — realm, application, roles,
  OAuth redirects, admin user, bootstrap API key)
- Generic Dockerfile + requirements.txt / package.json for app services
  without skeletons
- Deterministic `docker-compose.yml` (built from the plan, not parsed
  from an AI string)
- `.env` with template-contributed env vars grouped by prefix
- Built Docker images per app service

Free-port allocation and stale-image cleanup are also handled here.

### `AutoEngineer` (`bizniz/engineer/auto_engineer.py`)

**Per-service planner + dispatcher.** Calls AI for engineering analysis
(requirements, use cases, issues, architecture plan), runs deterministic
scaffold to write stub files, then runs the **three-phase strategy**:

- **Phase 1 (frame)** — cheapest model, every issue once with no tests,
  populates the workspace with real baseline code in topological order.
- **Phase 2 (escalate)** — for each model in `autocoder_models[1:]`,
  one attempt per still-failing issue with `max_iterations=2`.
- **Phase 3 (debug)** — for any remaining failures, the agentic
  debugger on `debugger_model` (default gemini-pro) with full tools
  (`view_file`, `list_directory`, `search_files`, `run_command`,
  `run_tests`) and `max_iterations=12`.

### `CodingOrchestrator` (`bizniz/orchestrator/coding_orchestrator.py`)

**Per-issue test/repair loop.** Coordinates Autocoder + Autotester +
optional AgenticDebugger inside a Docker test environment. Handles:
- Model escalation on stalls
- Stall recovery cycles (regenerate tests → flip strategy → full regen)
- Collection-error routing (source vs test, see
  [error_classification.md](architecture/error_classification.md))
- Config-aware repair — universal config files (jest.config,
  package.json, pyproject.toml, Dockerfile, etc.) are always writable
  for repair, not only when listed in `target_files`
- npm install propagation when `package.json` changes inside the Jest
  test container

### `CostTracker` (`bizniz/cost/`)

Captures every AI call (token counts, duration, USD cost) and persists
to `ProjectDB` as `jobs` + `api_calls` rows tagged with
`(job_id, service_name, issue_id, phase)`. Built-in rollups:
`cost_by_issue()`, `cost_by_service()`, `cost_by_model()`. See
[cost_tracking.md](architecture/cost_tracking.md).

---

## Data flow per build

1. **Architect.decompose** → one AI call → `SystemArchitecture`
2. **Provisioner.provision** → no AI → project on disk + Docker images
3. **For each service** (in dependency order):
   a. **AutoEngineer.analyze** → 3-pass AI: rough → plan → refined issues
   b. **Scaffold** → deterministic stub files
   c. **Phase 1: frame_issues()** → cheap-tier autocoder per issue, no tests
   d. **Phase 2: escalation chain** → one attempt per issue per model tier
   e. **Phase 3: agentic debug** → top tier with full tools (only if needed)
4. **Per issue, per phase, per model** — every call landed in `api_calls`

---

## Persistence

Every project gets two SQLite databases:

- `<project_root>/.bizniz/project.db` — project-level (`ProjectDB`):
  services, architecture snapshots, issue log, build events, drift
  events, **jobs**, **api_calls**.
- `<project_root>/<service>/.bizniz/bizniz.db` — workspace-level
  (`WorkspaceDB`) per service: problems, requirements, use cases,
  issues, architecture plans, namespaces/modules/dependencies, test
  results, environment packages.

A unified `BiznizDB` (MySQL or SQLite) is also available for
multi-project deployments but is opt-in.

---

## Key references

- [Pipeline sequence](pipeline_sequence.md) — step-by-step flow
- [Planner](architecture/planner.md) — milestone sequencing
- [Architect/Provisioner split](architecture/architect_provisioner_split.md)
- [Skeleton seeding](architecture/skeleton_seeding.md)
- [Cost tracking](architecture/cost_tracking.md)
- [Error classification (3b/3c)](architecture/error_classification.md)
- [Library docs index](README.md)
- [Per-run efficiency logs](runs/) — one `.md` per end-to-end run
- [Config reference](reference/config_reference.md)
- [Skeleton reference](reference/skeleton_reference.md)

---

## Design principles

- **Determinism first.** Templates beat AI for anything where structure
  matters (compose, Dockerfile, kickstart, requirements). AI is reserved
  for decisions that genuinely require judgment (decomposition,
  codegen, debugging).
- **One AI call per agent step.** Architect = one decompose call.
  Engineer = three structured passes. Orchestrator's repair = one inline
  call per iteration. No hidden multi-call loops.
- **Cost transparency.** Every AI call leaves a row. Cross-run analysis
  via SQL.
- **Skeletons over from-scratch.** Real working baselines (FastAPI auth,
  React+Vite, Angular+Material, Teams fan-out) bring tests + Docker +
  config so the AI only has to add app-specific code.
- **Recoverable failure.** Test loop catches regressions, config-aware
  repair fixes config files, three-phase strategy escalates only when
  cheap tiers fail. Stalls become explicit, not silent loops.
