# Evolve mode — milestone-driven incremental builds

The Planner ships a `ProjectPlan` = a sequence of `Milestone`s. Evolve
mode is the build path that walks those milestones one at a time,
incrementally extending the project at each step instead of starting
from scratch.

## Flow

```
problem statement
       │
       ▼
┌────────────────────┐
│      Planner       │   one AI call (gemini-pro)
│      .plan()       │   → ProjectPlan with N milestones
└─────────┬──────────┘
          │
          ▼
   for each milestone in sequence_index order:
     ┌──────────────────────────────────────────────┐
     │  set milestone.status = in_progress          │
     │  tracker.set_milestone(milestone.db_id)      │
     │                                              │
     │  ┌────────────────────────────────────────┐  │
     │  │ Architect.evolve(milestone, existing)  │  │  one AI call
     │  │  → updated SystemArchitecture          │  │
     │  │    (each service tagged                │  │
     │  │     new / extended / unchanged)        │  │
     │  └────────────────────────────────────────┘  │
     │                  │                           │
     │                  ▼                           │
     │  ┌────────────────────────────────────────┐  │
     │  │ Provisioner.evolve(architecture)       │  │  no AI
     │  │  - new services: full materialization  │  │
     │  │  - extended/unchanged: preserve files  │  │
     │  │  - rebuild compose deterministically   │  │
     │  │  - rebuild Docker images for new+ext   │  │
     │  └────────────────────────────────────────┘  │
     │                  │                           │
     │                  ▼                           │
     │  ┌────────────────────────────────────────┐  │
     │  │ Engineer dispatch on changed services  │  │  per-service
     │  │  (NEW + EXTENDED only)                 │  │  run_three_phase
     │  └────────────────────────────────────────┘  │
     │                                              │
     │  if all services pass:                       │
     │      milestone.status = completed            │
     │  else:                                       │
     │      milestone.status stays in_progress      │
     │      stop (unless continue_on_failure=True)  │
     └──────────────────────────────────────────────┘
          │
          ▼
   tracker.finish_job(status)
```

Entry point: `AutoArchitect.build_with_plan(problem_statement, project_name, ...)`.

## What's "evolve_state"?

Every `ServiceDefinition` carries an `evolve_state` field set by
`Architect.evolve()`:

| Value | Meaning |
|---|---|
| `new` | Service didn't exist before this milestone. Provisioner does full materialization (skeleton seed or app template, register in DB, build image). Engineer dispatches on it. |
| `extended` | Service existed; this milestone adds new endpoints / components / code. Provisioner does NOT re-seed the workspace (engineer-generated code stays put), but re-renders the infra Dockerfile if needed. Engineer dispatches on it. |
| `unchanged` | Service existed and the milestone doesn't touch it. Provisioner re-runs the infra template (idempotent, deterministic). Engineer skips it. |
| `None` | Treated as `unchanged` defensively. |

`AutoArchitect.decompose()` (the fresh / non-evolve path) tags every
service it produces as `new` so downstream code can rely on the field.

## Architect.evolve

```python
evolved = architect.evolve(
    milestone=milestone,
    existing_architecture=current_arch,
    problem_statement=full_problem,   # for context
    project_name="Mini CRM",
)
```

One AI call (top-tier, default `gemini-pro`). The prompt:
- Echoes the existing architecture (every service with name, type,
  framework, language, port, depends_on, skeleton).
- Names the milestone, its problem_slice, use cases, success criteria.
- Instructs: every existing service MUST appear in the response (don't
  drop anything). Each service gets `evolve_state`. Existing services
  keep their identity (framework/language/port/skeleton); only
  `requirements` and `depends_on` may be extended.

Defenses on the parse side:
- **Identity preservation:** for any service whose name matches one in
  `existing_architecture`, the output keeps the prior service's
  framework, language, port, skeleton, and image_name. AI attempts to
  rewrite those fields are ignored. New requirements/depends_on merge.
- **Drop recovery:** if the AI omits an existing service, it's
  re-inserted with `evolve_state="unchanged"` and a log line.

## Provisioner.evolve

```python
result = provisioner.evolve(architecture, project_name)
```

No AI. Idempotent re-provision. Differences vs. the fresh `provision()`:

| Concern | provision() | evolve() |
|---|---|---|
| Project cleanup (remove prior images) | Yes | **No** — would delete completed milestones |
| Skeleton seeding | All app services | **Only `evolve_state="new"`** services |
| App template render (Dockerfile etc.) | All app services | Only `new` |
| Infrastructure template render | All infra | All infra (idempotent — pure function) |
| Compose regeneration | Always | Always (deterministic from architecture) |
| `.env` regeneration | Always | Always |
| Free-port allocation | All host-port-bearing services | Only `new` services (existing keep their ports) |
| Docker image build | All app services | Only `new` and `extended` |
| Architecture snapshot | One initial | Appended each evolve call |

Skeleton-seeded workspaces with engineer-generated code from prior
milestones are **not** trampled. The Dockerfile in
`infra/development/<svc>/` is refreshed if it drifted from the
workspace's source, but app code stays put.

## Director loop (`AutoArchitect.build_with_plan`)

The top-level entry point that ties planner + evolve calls together:

```python
results = architect.build_with_plan(
    problem_statement="Build a CRM with auth, contacts, deals, reporting",
    project_name="Mini CRM",
    parallel=False,
    layered=True,
    continue_on_failure=False,
)
```

For each milestone:
1. Mark `in_progress` in the milestones table.
2. `tracker.set_milestone(milestone.db_id)` — every AI call from here
   on attaches this milestone_id to its `api_calls` row, enabling
   `cost_by_milestone()` rollups.
3. `tracker.set_phase("architect.evolve")` then call `evolve()`.
4. `tracker.set_phase("provisioner.evolve")` then call provisioner.
5. Filter to changed services (`evolve_state in {new, extended}`,
   `service_type in {backend, frontend, worker}`).
6. Dispatch engineers on changed services in dependency order. Engineer
   uses `milestone.problem_slice` (not the full project problem) so the
   issue list stays milestone-scoped.
7. If all services pass → mark milestone `completed`.
   If any failed → log build_event, leave milestone `in_progress`,
   stop unless `continue_on_failure=True`.
8. After the loop, `tracker.finish_job(status)`.

## Failure semantics

- Default: stop at the first failed milestone. The milestone stays
  `in_progress`; the surrounding job is marked `failed`. The user can
  fix what broke and re-run.
- `continue_on_failure=True`: log the failure and proceed to the next
  milestone. Useful for milestones that don't block each other (rare —
  most milestones depend on prior ones), or for diagnostic runs.
- A crash in `Architect.evolve` or `Provisioner.evolve` is also treated
  as a milestone failure.
- The job's cost summary is logged in the `finally` block so failed
  runs still produce a complete cost record.

## Cost rollups by milestone

Once evolve mode runs, `api_calls` rows carry `milestone_id` for every
call made inside the milestone iteration (architect.evolve,
provisioner.evolve, engineer.analyze, autocoder/autotester per phase).
`ProjectDB.cost_by_milestone(plan_id)` aggregates:

```python
for row in project.db.cost_by_milestone(plan.db_id):
    print(f"#{row['sequence_index']} {row['name']}: "
          f"{row['calls']} calls, ${row['total_cost']:.4f}")
```

Milestones with zero calls (planned but not started) appear with zero
rollup, which is correct and useful (you can see what's queued).

## Milestone status state machine

```
planned ─────► in_progress ─────► completed
                   │
                   └────► (failure) stays in_progress
                              + project.db.log_build_event(...)
                              + job.status = failed
```

`skipped` is a valid status the user can set manually but the build
loop never assigns it. (It's there for "mark this milestone as done
without running it" — useful when re-running a partially-shipped plan.)

## What's NOT in this branch

- **Engineer scope reduction.** When milestone 2 extends an existing
  service, the engineer's `analyze()` runs from scratch on
  `milestone.problem_slice`. It naturally produces only milestone-
  relevant issues (the slice is narrow), but if it proposes to
  re-implement something already shipped, framing + the test loop
  catch that quickly. A future optimization is to feed the engineer
  the existing issue list as context.
- **Re-plan during a build.** If a milestone fails or scope shifts,
  you'd want to re-plan from the current state. Not yet wired —
  re-plan is a manual step (call `Planner.plan(...)` again with
  `existing_architecture` set; it archives the old plan).

## Tests

- 6 unit tests for `Architect.evolve` (mocked client) — preservation,
  drop recovery, empty-existing case, decompose tags new.
- 8 unit tests for `Provisioner.evolve` — workspace preservation, port
  allocation, idempotent compose regeneration, evolve_state=None.
- 1 functional test for `Architect.evolve` against real Gemini —
  notes-app extension on top of an auth+profile baseline; verifies
  identity preservation and that the AI tags at least one service as
  new/extended.

```
pytest bizniz/architect/tests/test_evolve.py bizniz/provisioner/tests/test_evolve.py
pytest -m functional bizniz/architect/tests/functional/test_evolve_real.py
```
