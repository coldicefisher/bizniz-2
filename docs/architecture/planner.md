# The Planner

The Planner sits above the Architect. It takes a problem statement and
produces an ordered sequence of **milestones**, each a self-contained
slice of user value. The Architect later runs once per milestone and
maps each milestone's deliverables onto services + code.

The Planner does **not** decide which services to build, which
frameworks to use, or what file structure to use. Those are the
Architect's concerns. The Planner reasons in product terms — use
cases, success criteria, sequencing — and the Architect reasons in
engineering terms — services, ports, dependencies.

## Where it fits

```
problem statement
       │
       ▼
┌────────────────────┐
│      Planner       │   one AI call (gemini-pro)
│      .plan()       │   → ProjectPlan with N ordered Milestones
└─────────┬──────────┘
          │  for each milestone (current scope: A+E only — Planner
          │  is built but the Architect doesn't yet consume it.
          │  Evolve-mode wiring lands in a follow-up branch.)
          ▼
┌────────────────────┐
│   Architect    │   one AI call per milestone
│   .decompose()     │   → SystemArchitecture for THIS milestone's slice
└─────────┬──────────┘
          ▼
       (provisioner, engineer, orchestrator as documented elsewhere)
```

## Milestone shape

Each `Milestone` is a self-contained problem-slice:

| Field | What it is |
|---|---|
| `sequence_index` | 0-based ordering. Stable-sorted on read so AI numbering glitches don't propagate. |
| `name` | Short label, 3–6 words. ("Auth + profile", "Deal pipelines", "Reporting v1".) |
| `problem_slice` | Self-contained problem statement just for this milestone. The Architect can read it standalone and decompose without re-reading the full project problem. |
| `use_cases` | User stories shipped: "user can sign up", "user can log in". |
| `success_criteria` | Testable outcomes from the user's perspective. |
| `depends_on_names` | Other milestone names from THIS plan that must ship first. |
| `estimated_effort` | `S` (a few days), `M` (~1 week), `L` (1–2 weeks). Human review hint, not enforced. |
| `status` | `planned` → `in_progress` → `completed` (or `skipped`). |

A `ProjectPlan` is just a project_slug + problem_statement +
description + ordered list of Milestones.

## Public API

```python
from bizniz.planner import Planner

planner = Planner(client=top_tier_client, environment=env, workspace=ws)
plan = planner.plan(
    problem_statement="Build a CRM ...",
    project_name="Mini CRM",
    project_db=project.db,           # optional — persists the plan
    existing_architecture=arch,      # optional — re-plan against existing
    completed_milestones=[...],      # optional — context for re-plans
)
for m in plan.milestones:
    print(m.sequence_index, m.name, m.use_cases)
```

The first call on a project produces a fresh plan. A second call
re-plans against the existing project: the prior active plan is
archived (kept in `project_plans` with `archived_at` set) and a new
active plan replaces it. `project.db.get_active_plan(project_slug)`
always returns the newest non-archived plan.

## Persistence (project DB)

Two new tables in `ProjectDB`:

```sql
project_plans
  id, project_slug, problem_statement, description,
  created_at, archived_at

milestones
  id, plan_id (FK), sequence_index, name, problem_slice,
  use_cases_json, success_criteria_json, depends_on_json,
  estimated_effort, status, started_at, completed_at, created_at
```

Plus a new column on `api_calls`:

```sql
api_calls.milestone_id INTEGER  -- nullable
```

`CostTracker.set_milestone(id)` tags subsequent records with the
active milestone, so once evolve-mode is wired in,
`project.db.cost_by_milestone(plan_id)` rolls up cost per milestone.
For now (A+E only) `milestone_id` stays NULL on records — the rollup
returns 0-cost rows for every planned milestone, which is correct
("not started yet").

`ProjectDB` methods:

| Method | Purpose |
|---|---|
| `save_project_plan(slug, problem_statement, description)` | insert and return id |
| `archive_plan(id)` | set `archived_at` on the prior active plan |
| `get_active_plan(slug)` | most recent non-archived plan |
| `save_milestone(plan_id, sequence_index, name, problem_slice, use_cases, success_criteria, depends_on_names, estimated_effort, status)` | insert and return id |
| `get_milestones(plan_id, status=None)` | ordered by sequence_index |
| `get_milestone(id)` | single row |
| `update_milestone_status(id, status)` | transitions; sets `started_at` on `in_progress`, `completed_at` on `completed` |
| `cost_by_milestone(plan_id=None)` | LEFT JOIN api_calls; planned-but-not-started milestones appear with zero rollup |

## Model tier

Default: `gemini-pro` (top tier).

Reasoning: the Planner runs **once per project** (or rarely on
re-plans). One top-tier call costs ~$0.05–0.30 — rounding error
relative to the rest of a build. The quality bump on a foundational
decision is large. A weak Planner = wrong sequencing = weeks of
wasted work; a slightly weaker Architect for a single milestone is
recoverable when that milestone's tests fail.

Configured in `bizniz.yaml` and `BiznizConfig`:

```yaml
planner_model: gemini-pro
```

```python
config.planner_model           # str
config.make_planner_client()   # → BaseAIClient
```

## Prompt design

System prompt (full text in
`bizniz/planner/prompts/system_prompt.py`):

- States the role: take a high-level problem and decompose it into
  1–2 week milestones.
- Says explicitly: do NOT decide services, frameworks, databases,
  file structure — that's the Architect's job.
- Gives heuristics: 4–8 milestones is typical, first milestone is
  usually auth + simplest core entity, no infra/test/deploy chores.

User prompt (in `bizniz/planner/prompts/plan_prompt.py`):

- Echoes the problem statement and project name/slug.
- Lists each Milestone field with explicit definitions of
  `problem_slice` (must be self-contained), `use_cases`, etc.
- Optionally appends an "existing state" block for re-plans, listing
  the current architecture and already-completed milestones.

Schema (in `bizniz/planner/prompts/schema.py`) — strict JSON schema
locking down the response structure.

## Testing

- 14 unit tests with mocked client cover: ordering / stable sort /
  empty-response retry / use-case + success-criteria preservation /
  re-plan archive flow / persistence + rollup.
- 2 functional tests against real Gemini verify auth-comes-first,
  contacts-before-deals, all dependency names exist in the plan, and
  end-to-end DB persistence.

Run unit only:

```
pytest bizniz/planner/tests/test_planner.py
```

Run functional (needs `GEMINI_API_KEY`):

```
pytest -m functional bizniz/planner/tests/functional/
```

## What's NOT yet wired

Currently the Planner is **standalone**. `architect.build()` does not
call it; the Architect still decomposes from the full problem
statement directly. Wiring the Planner into the build loop —
"for each milestone in plan: architect.evolve(milestone) →
provisioner.evolve() → engineer per service" — is the next branch
(`feat/evolve-mode`).

Until evolve-mode lands, the Planner is useful for:
- Producing a `ProjectPlan` doc you can review before kicking off a
  build, so you can see how the AI would slice the problem.
- Persisting plans to the project DB for reference.
- Cost-rollup tooling (the `cost_by_milestone` query works today; it
  just shows zeros until evolve-mode tags `api_calls` with milestones).
