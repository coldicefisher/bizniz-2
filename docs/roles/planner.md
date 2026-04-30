# Planner

The top-of-stack sequencing agent. `bizniz/planner/planner.py` defines an
agent that takes a high-level problem statement and produces an ordered
sequence of **milestones** — chunks of user value the rest of the
pipeline can build incrementally.

## Purpose

The Planner reasons in product terms: "what does the user get first?
what depends on what? how do I cut this multi-week project into 1–2
week deliverables?" It does NOT decide which services to build, which
frameworks to use, or what file structure to use. Those are the
[Architect](architect.md)'s concerns, run once per milestone.

For a CRM problem statement, a typical Planner output:

| # | Milestone | Use cases |
|---|---|---|
| 0 | Auth + profile | sign up, log in, view profile |
| 1 | Contact CRUD | add/edit/delete/view contacts |
| 2 | Deals on contacts | attach deals, set stage/value |
| 3 | Pipelines | drag deals between stages |
| 4 | Reporting | dashboards, totals, charts |

Each milestone is a self-contained problem-slice the Architect can
decompose without reading the full project problem.

## Constructor

| Parameter | Type | Notes |
|-----------|------|-------|
| `client` | `BaseAIClient` | AI provider for the planning call. Default tier: `gemini-pro` (top tier). |
| `environment` | `BaseExecutionEnvironment` | Required by `BaseAIAgent`; not invoked here. |
| `workspace` | `BaseWorkspace` | Used for system-prompt fixtures only. |
| `max_retries` | `int = 3` | Number of retries on AI failure. |
| `on_event`, `on_status_message` | callbacks | Standard agent callbacks. |

## Public API

### `plan(problem_statement, project_name, *, existing_architecture=None, completed_milestones=None, project_db=None) → ProjectPlan`

The single entry point. One AI call.

| Parameter | What it does |
|---|---|
| `problem_statement` | Natural-language description of what to build. |
| `project_name` | Human-readable name; slugified for `project_slug`. |
| `existing_architecture` | Optional `SystemArchitecture` for re-plans. When set, the prompt switches to "you're re-planning the remaining work" mode and the AI is told what already exists. |
| `completed_milestones` | Optional `List[Milestone]` of already-shipped chunks for re-plans. |
| `project_db` | Optional `ProjectDB`. When provided, the new plan is persisted (and any prior active plan for the same slug is archived first). |

Returns a `ProjectPlan` with sorted-by-`sequence_index` milestones.

### `Milestone` shape

```python
@dataclass
class Milestone:
    db_id: Optional[int] = None
    sequence_index: int = 0
    name: str                       # short label, 3–6 words
    problem_slice: str              # self-contained problem statement
    use_cases: List[str] = []       # user stories
    success_criteria: List[str] = []  # testable outcomes
    depends_on_names: List[str] = []  # other milestone names
    estimated_effort: Optional[str] = None  # S | M | L
    status: str = "planned"         # planned | in_progress | completed | skipped
```

`ProjectPlan` wraps an ordered list:

```python
@dataclass
class ProjectPlan:
    db_id: Optional[int] = None
    project_slug: str
    problem_statement: str
    description: str = ""           # 1-2 sentence overview
    milestones: List[Milestone] = []
```

## Prompt design

Three files in `bizniz/planner/prompts/`:

- `system_prompt.py` — role definition + heuristics: 4–8 milestones is
  typical, first milestone usually auth + simplest core entity, no
  infra/test/deploy chores, names short (3–6 words), use cases in
  user-story form.
- `plan_prompt.py` — user prompt + an `EXISTING_STATE_TEMPLATE` block
  that's appended for re-plans. The block lists the existing
  architecture and already-completed milestones so the AI proposes
  delta milestones, not a full rebuild.
- `schema.py` — strict JSON schema locking down the response shape.

## Defenses against AI drift

- **Stable sort by `sequence_index`** on parse, so AI numbering glitches
  (returning out-of-order milestones) don't propagate.
- **Empty-milestone-list rejection** — `PlannerBadAIResponseError` if
  the AI returned no milestones.
- **Retry-on-empty** — up to `max_retries` (default 3) AI calls before
  giving up.

## Persistence

Two tables in `ProjectDB` (see [modules/project.md](../modules/project.md)):

- `project_plans` — one row per plan. `archived_at` set when a re-plan
  supersedes the previous one.
- `milestones` — rows per plan, ordered by `sequence_index`.

Methods on the DB ([reference](../modules/project.md)):

| Method | Purpose |
|---|---|
| `save_project_plan(...)` | insert and return id |
| `archive_plan(plan_id)` | set `archived_at` on prior active plan |
| `get_active_plan(project_slug)` | most recent non-archived plan |
| `save_milestone(...)` | insert and return id |
| `get_milestones(plan_id, status=None)` | ordered by sequence_index |
| `get_milestone(id)` | single row |
| `update_milestone_status(id, status)` | transitions; sets `started_at` / `completed_at` automatically |
| `cost_by_milestone(plan_id=None)` | LEFT JOIN api_calls; planned-but-not-started milestones appear with zero rollup |

## Integration with the build path

The Planner is the first step of [evolve_mode.md](../architecture/evolve_mode.md).
`Architect.build_with_plan(problem_statement, project_name, ...)`:

1. Allocates a cost-tracker job_id.
2. Calls `Planner.plan(...)` (skipped if a pre-built plan is passed).
3. Walks milestones in `sequence_index` order.

For each milestone the director loop calls `Architect.evolve()` →
`Provisioner.evolve()` → engineer dispatch on changed services. Every
AI call inside the milestone iteration is tagged with `milestone_id`
on its `api_calls` row.

The Planner is also callable standalone — useful for producing a plan
to review before kicking off a build. `Architect.build()` (the
single-shot path) doesn't call the Planner.

## Model tier

Default: `gemini-pro` (top tier).

Reasoning: the Planner runs **once per project** (or rarely on
re-plans). One top-tier call costs ~$0.05–0.30 — rounding error
relative to the rest of a build, while the quality bump on a
foundational sequencing decision is large. A weak Planner = wrong
sequencing = weeks of wasted work.

Configured in `bizniz.yaml` and `BiznizConfig`:

```yaml
planner_model: gemini-pro
```

```python
config.planner_model           # str
config.make_planner_client()   # → BaseAIClient
```

## Tests

| File | Coverage |
|---|---|
| `bizniz/planner/tests/test_planner.py` | 14 unit tests with mocked client — ordering, stable sort under AI scrambling, empty-response retry, use-case + success-criteria preservation, `existing_architecture` re-plan flow, persistence + archive, milestone-status transitions, cost-by-milestone rollup |
| `bizniz/planner/tests/functional/test_planner_real.py` | 2 functional tests against real Gemini — CRM problem produces auth-first sequencing + contacts-before-deals + valid dependency names; end-to-end DB persistence verified |

```
pytest bizniz/planner/tests/test_planner.py
pytest -m functional bizniz/planner/tests/functional/   # needs GEMINI_API_KEY
```

## Cross-references

- [architecture/planner.md](../architecture/planner.md) — design rationale, milestone shape, module layout
- [architecture/evolve_mode.md](../architecture/evolve_mode.md) — the build path the Planner feeds into
- [roles/architect.md](architect.md) — the next agent down (`Architect.evolve` consumes one milestone at a time)
