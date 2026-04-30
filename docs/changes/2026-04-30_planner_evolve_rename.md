# 2026-04-30 (later) — Planner + evolve mode + Auto* rename

Three substantive changes shipped on top of the morning's
provisioner-split work.

## 1. Planner agent (commit `ebf153c`, merged `f7faaeb`)

A new top-of-stack agent that sequences user value into ordered
milestones. Sits above the Architect.

```python
from bizniz.planner import Planner, ProjectPlan, Milestone

plan = planner.plan(problem_statement, project_name)
for m in plan.milestones:
    print(m.sequence_index, m.name, m.use_cases)
```

The Planner reasons in product terms (use cases, success criteria,
sequencing). It does NOT decide services / frameworks / file structure
— those are the Architect's concerns, run once per milestone in
evolve mode.

**Persistence.** Two new tables in `ProjectDB`:
- `project_plans` — id, project_slug, problem_statement, description,
  created_at, archived_at
- `milestones` — plan_id, sequence_index, name, problem_slice,
  use_cases_json, success_criteria_json, depends_on_json,
  estimated_effort, status, started_at, completed_at, created_at

Plus `milestone_id` column added to `api_calls` (with idempotent
migration) so cost rollups can group by milestone.

**Model tier.** `planner_model: gemini-pro` (top tier) added to
`BiznizConfig` and `bizniz.yaml`. `architect_model` also bumped from
`gemini-flash` to `gemini-pro` — both are one-shot calls per project,
the cost increment is negligible vs. the quality bump on foundational
decisions.

**Tests.** 14 unit tests (mocked client) + 2 functional tests
(real Gemini, CRM problem) all pass.

**Doc:** [`docs/roles/planner.md`](../roles/planner.md) (per-agent
reference), [`docs/architecture/planner.md`](../architecture/planner.md)
(design rationale).

## 2. Evolve mode (commit `35f0873`)

`Architect.build_with_plan(problem, project_name, ...)` walks the
Planner's `ProjectPlan` one milestone at a time, evolving the project
incrementally instead of rebuilding from scratch.

**`ServiceDefinition.evolve_state`** — new field set by
`Architect.evolve()`:
- `new` — service didn't exist before this milestone
- `extended` — service existed; this milestone adds new code
- `unchanged` — service exists, milestone doesn't touch it
- `None` — treated as `unchanged` defensively

`Architect.decompose()` (the fresh-build path) tags every service
`new` so downstream code can rely on the field.

**`Architect.evolve(milestone, existing_architecture, ...)`** — one
AI call (top-tier). Defenses on the parse side:
- Identity preservation: existing services keep their original
  framework / language / port / skeleton; only `requirements` and
  `depends_on` may merge.
- Drop recovery: if the AI omits an existing service, it's re-inserted
  with `evolve_state="unchanged"`.

**`Provisioner.evolve(architecture, project_name)`** — idempotent
re-provision. Differences from `provision()`:

| Concern | provision() | evolve() |
|---|---|---|
| Image cleanup | Yes | No (would delete prior milestones' work) |
| Skeleton seeding | All app services | Only `new` |
| Free-port allocation | All host ports | Only `new` services |
| Compose regeneration | Always | Always (deterministic) |
| Docker image build | All | Only `new` + `extended` |

**Director loop** — `Architect.build_with_plan()`:
1. Open cost-tracker job.
2. Planner.plan() (or use pre-supplied plan).
3. For each milestone in `sequence_index` order:
   - `tracker.set_milestone(milestone.db_id)` — every AI call from
     here gets `milestone_id` stamped on its `api_calls` row.
   - Architect.evolve → Provisioner.evolve → engineer dispatch on
     changed services only (NEW + EXTENDED, app types).
   - Mark milestone `completed` (or stay `in_progress` on failure;
     stop unless `continue_on_failure=True`).
4. tracker.finish_job(status).

**Tests.** 6 unit tests for `Architect.evolve` + 8 for
`Provisioner.evolve` + 1 functional test against real Gemini (notes-
app extension). All pass.

**Doc:** [`docs/architecture/evolve_mode.md`](../architecture/evolve_mode.md).

## 3. Drop the Auto* prefix (commit `a431648`, merged `182795f`)

Brings agent class + module names in line with the newer Planner
naming. The `Auto*` prefix was redundant ("autonomous AI agent" is
true of every agent) and had drifted from how everyone refers to them
in conversation.

| Before | After |
|---|---|
| `AutoArchitect` | `Architect` |
| `AutoEngineer` | `Engineer` |
| `Autocoder` | `Coder` |
| `Autotester` | `Tester` |
| `Autodebugger` | `QuickDebugger` (canonical class was already
  `QuickDebugger` in `agents/debugger/quick.py`; the `Autodebugger`
  shim is gone) |
| `AgenticDebugger` | unchanged (descriptive) |
| `AutoStub` | unchanged (preflight stubs, different concept) |

**Module/file renames** mirror the class rename:
- `bizniz/architect/auto_architect.py → architect.py`
- `bizniz/engineer/auto_engineer.py → engineer.py`
- `bizniz/agents/autocoder/ → agents/coder/`
- `bizniz/autotester/ → bizniz/tester/`

**Symbol renames** — every error type, schema, system-prompt
constant, and OnEventCallback that used `Auto*` was swept. Plus
lowercase identifiers (parameter / attribute / config-key names).

**Shims deleted** — `bizniz/autocoder/`, `bizniz/autodebugger/`,
`bizniz/agentic_debugger/`. Test coverage for the old `Autodebugger`
shim was preserved by restoring the test under
`bizniz/agents/debugger/tests/test_quick_debugger.py`.

**Pytest gotcha.** Classes named `Tester*` matched pytest's default
"Test*" discovery rule. Added `__test__ = False` to `Tester`,
`TesterResult`, `TesterError`, `TesterBadAIResponseError`,
`TesterOnEventCallback` so pytest skips them.

**Numbers.** 198 paths changed, 632 non-functional tests pass
(unchanged from before the rename — zero regressions).

## Tests + plan status

- 632 non-functional tests pass
- 17 functional tests deselected (real-AI, real-Docker; not affected
  by these changes)

| # | Item | Status |
|---|---|---|
| 1–8 | Skeleton wiring, framing, three-phase, bug fixes, library docs, cost analysis | ✅ DONE |
| 9 | Planner agent (A) | ✅ **DONE 2026-04-30** |
| 10 | Planner DB schema (E) | ✅ **DONE 2026-04-30** |
| 11 | Architect.evolve | ✅ **DONE 2026-04-30** |
| 12 | Provisioner.evolve | ✅ **DONE 2026-04-30** |
| 13 | `build_with_plan` director loop | ✅ **DONE 2026-04-30** |
| 14 | Auto* → bare-name rename | ✅ **DONE 2026-04-30** |
| future | Real end-to-end `build_with_plan()` smoke against a 2-milestone problem | not started |
| future | Engineer scope reduction (skip already-implemented issues during evolve) | not started |
| future | Cross-project rollup via unified `BiznizDB` | not started |
| future | Re-score historical `api_calls` after pricing change | not started |

## What hasn't been exercised yet

The Planner, evolve-mode, and rename are all **individually tested**
(unit + functional). What hasn't run yet is a **full vertical slice**:
real-Gemini Planner → real-Gemini Architect.evolve →
Provisioner.evolve (real Docker) → Engineer dispatch (real codegen)
across two milestones, with cost rollup captured.

That's the natural next step — write a `docs/runs/` log capturing it.
