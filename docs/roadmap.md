# Bizniz Roadmap — 2026-05-15 onward

Locked in as of 2026-05-15 after the CRM v1 build surfaced enough
gaps to warrant explicit sequencing. **Work the items in order** —
each one's value compounds on the prior items.

## The 10-item plan

### 1. Confidence signals — load-bearing or drop the pretense ✅ SHIPPED

**Shipped 2026-05-15** (commit `5de1059`). Three bands now drive
harness behavior:

- **≥ 0.6**: implement normally
- **0.4-0.6**: harness runs one `QualityEngineer.re_enrich` pass
  with an augmented "name your ambiguities, resolve or surface as
  TODOs" prompt. Take whichever spec has higher confidence.
- **< 0.4**: fires `enrich_low_confidence` soft gate. Halts in
  `--interactive`, warns + continues in `--auto`/strict.

Pieces shipped: `QualityEngineer.re_enrich`, `build_reenrich_prompt`,
`MilestoneLoop._maybe_re_enrich` with `confidence_low_threshold`
(0.6) + `confidence_halt_threshold` (0.4) constructor params,
updated ENRICH_SYSTEM_PROMPT, +11 unit tests.

The meta-pattern audit + retrofit for the other agents
(CodeReviewer, Coder, Tester, Architect, Planner) is now part of
item 7's diagnostic logging push — same `AgentConfidence` shape,
universal harness behavior off it.

Full ticket: `docs/backlog/confidence_signals.md`.

### 2. Finish UX with Storybook

Get the React-Vite UX loop end-to-end on Storybook so the
interaction-test phase (Ticket 3 of the UX backlog) becomes the
default UX gate, not the screenshot-only loop. Today we have:

- ✅ Storybook scaffolding in `bizniz-skeleton-react`
- ✅ Engineer prompt requires `.stories.tsx` per primitive
- ✅ `v2_build` routes UX through `ProUXDesigner` (not legacy)
- ⏳ ProUXDesigner consumes stories — currently it still
  screenshots routes, not stories
- ⏳ Angular skeleton variants (skeleton-angular, teams, saas)

**Done when:** UX phase iterates the Storybook catalog, scores per
primitive, dispatches Coder per primitive (not per route).

### 3. Add version control ✅ SHIPPED

**Shipped 2026-05-15** (commit `8c65435`). Per-project git
checkpoints at every phase boundary:

- After Provisioner → `git init` + commit "Initial provision"
  tagged `m0`
- After each MilestoneLoop.run() → commit "M<N>: <name> DONE"
  tagged `m<N>-done`

`ProjectGit.revert_to_tag(...)` provides the rollback path the
refactorer (item 5) needs. `.bizniz/` is tracked (so reverts roll
back internal state coherently). All ops are best-effort: git
failures never tank the pipeline. +18 tests.

Phase-level commits (planner / architect / per repair iter / per
integration) NOT wired yet — milestone DONE checkpoints are
sufficient for refactor revert. If finer granularity is needed
later, add per-phase commits to `MilestoneLoop` callsites.

### 4. Granular issue decomposition via Decomposer agent

CRM v1 timing data (2026-05-15): Coder subprocess time dominated
(545 min across 130 calls, p95 572s, max 1072s ≈ 18 min). Root
cause: ServicePlanner emits feature-sized issues that bundle 3-5
files of work into one Coder pass. The model under-attends, debug
blast radius is wide, refactor extractions can't be atomic.

**Decision (2026-05-15):** rather than shrinking ServicePlanner's
issues (which would lose the "feature-as-unit" semantic), add a
NEW phase between ServicePlanner and Coder: a **Decomposer** agent
that breaks each issue into an ordered list of **units of work**.

**The new dispatch loop:**

```
ServicePlanner: backend → 8 issue(s) in 4 layer(s)
  ↓
For each issue:
  Decomposer.decompose(issue, workspace, architecture)
    → ordered List[UnitOfWork]
  ↓
  For each unit (in dependency order):
    Coder writes the unit (and its test inline for v1)
    Run unit test → if fail, debugger
    Mark unit complete
  ↓
  Issue complete when all units pass
```

**Unit of work shape (working definition):**

- ONE new exported symbol (function, class, component, route) OR
  ONE new behavior added to an existing symbol
- Bounded to one file ideally; pure boilerplate (imports, types,
  constants) bundles with the symbol that needs it
- Has explicit `depends_on` listing prior unit IDs OR existing
  workspace symbols
- Has `expected_test_kind` (`unit_test` / `no_test_needed`) so the
  loop knows whether to require a passing test before moving on

**Why a separate agent (vs Coder self-decomposing):**

- Clean separation of concerns — decomposition is its own
  judgment, distinct from "write the code"
- Cheap to test in isolation (decompose without coding)
- Easy to A/B test decomposition strategies later (different
  prompts, different models)
- Naturally pluggable across LLM backends — same as our other
  single-call agents

**Done when:**

1. `bizniz/decomposer/` package: `Decomposer` agent + types +
   prompts. Single-call (`claude --print --output-format=json`)
   pattern matching our other agents.
2. `MilestoneCodeDispatcher` calls Decomposer for each issue
   before dispatching units to Coder.
3. Coder loop runs per-unit (not per-issue); test failure halts at
   the broken unit.
4. p95 Coder-per-unit drops below 180s; p50 around 90s. Total
   Coder time roughly flat — split into more, smaller pieces.
5. Refactor extractions (item 5) consume one unit per commit
   cleanly.

**v1 scope cuts (defer to follow-ups):**

- Per-unit Tester separation (today's Coder writes code+test
  inline; that stays for v1). Splitting Tester out is its own
  micro-ticket once the Decomposer + loop are proven.
- Resume tracking at unit granularity. Today's MilestoneState
  tracks per-issue. v1 redoes all units of an issue on resume
  (idempotent-ish — Coder skips already-correct code).
- Parallel unit dispatch within independent dependency leaves.
  Sequential for v1; parallelism is a tier-2 optimization once
  serial works.

**Why between version control and refactor:** smaller units benefit
every downstream Coder-driven step. Item 5's extractions become
naturally atomic (one unit = one commit). Item 8's perf logging
sees per-unit granularity, which makes baseline data actionable.

### 5. Refactorer agent — dedupe + move to shared core

We have a v1 Refactorer (single Claude CLI session at the project
root). It works minimum-viable but doesn't:
- Move API code to shared core libraries when business logic
  duplicates across services
- Detect cross-service abstraction opportunities
- Coordinate with version control to commit each extraction
  separately

**Done when:** a multi-service project's business logic lives in
`shared/<lang>/` libraries, consumer services import them, tests
pass, each extraction is its own commit (cleanly granular thanks
to item 4).

### 6. Tests / debugging after refactoring

Refactors can break things. After (5) runs, drive a focused
test+repair loop:
- Run every service's test suite
- On failure, dispatch the AgenticDebugger
- Roll back via git if convergence fails

**Done when:** refactorer-induced regressions are caught + fixed
automatically, not by humans noticing later.

### 7. Human documentation system

Agents write semantic documentation for the generated project:
- README.md per service (what it does, how to run it)
- API reference (auto-generated from OpenAPI + narrative)
- Architecture overview (services + interactions)
- "How to extend" guides per skeleton-shipped extension point

**Why this order:** the docs need to describe the *post-refactor*
shape, not the pre-refactor shape. Documenting twice is waste.

### 8. Detailed diagnostic + performance logging

Wire structured logging across every agent:
- Per-call timing (already partial via cost tracker)
- Token usage breakdown
- Tool-loop step counts
- Per-issue convergence path (which tier, which iter, why)
- Repair sticky-log compaction stats
- Cache hit rates (plan cache, route resolver, primitive probes)
- **Confidence-signal audit + retrofit** for the agents that don't
  self-rate yet (Architect, Planner, Coder, Tester) — same shape as
  item 1 but extended across the pipeline.

**Done when:** a single `bizniz_projects/<slug>/docs/runs/<job_id>/
performance.json` answers "where did this build spend time/tokens
and what could be cheaper."

### 9. Performance test on Claude

Build 3-5 reference projects (CRM, blog, e-commerce mini, ...) on
Claude with the full instrumentation from (8). Establish baselines:
- Wall clock per milestone
- Token cost per service per milestone
- Repair iterations needed
- UX score per route
- Refactor extractions per multi-service project

**Why on Claude first:** $0 marginal on Max plan lets us iterate
without budget pressure during baseline-finding.

### 10. Baseline on Gemini

Run the same reference projects on Gemini, compare against (9)'s
Claude baselines:
- Where does Gemini close the gap?
- Where does it widen?
- What prompt / agent tweaks move the needle?

**Goal:** durable architecture comparison, not a one-off run. Drives
where to invest next.

## Order rationale

- **1 first** because it's the cheapest quality lever — preventing
  burnt cycles on ambiguous specs costs basically nothing to wire
  and earns every downstream item more reliable inputs.
- **2 next** because UX is the most user-visible quality bar — the
  thing buyers see — and Storybook is the right shape.
- **3 → 4 → 5 → 6** is the refactor-safety stack. Can't do refactor
  (5) without version control (3); item 4 (granular issues) means
  each refactor extraction is one atomic commit; item 6 catches
  what 5 misses.
- **7 after 5** so docs reflect the refactored shape, not the
  pre-refactor sprawl.
- **8 before 9** so the baseline data exists in structured form.
- **9 before 10** so we have Claude numbers to compare Gemini against.

## What's NOT in this plan (deferred)

- Angular skeletons (skeleton-angular, teams, saas) get Storybook
  scaffolding only AFTER item 2 proves the React loop end-to-end.
- Full escalation on smoke gate (replace hard-fail with cheap-tier
  AgenticDebugger one-shot) — ALREADY SHIPPED in commit `29e5ea9`
  via `SmokeRecovery`. Future work: extend to multi-tier escalation
  inside roadmap item 6 if one-shot isn't enough on real failures.
- Production-mode Dockerfile variants (no `--reload`, Alembic
  migrations) — deferred until the dev-mode loop is stable.

## Feedback baked in this session

- On smoke failures, prefer full agent escalation (try to recover
  the container) rather than hard-halt at the gate. **Shipped
  2026-05-15 (`29e5ea9`)** — one-shot `SmokeRecovery` agent runs
  before hard-halt. Multi-tier escalation deferred to item 6.
- Agent self-rated confidence fields should be load-bearing or
  removed from prompts. **Now item 1** of this roadmap (shipped
  2026-05-15, commit `5de1059`).
- Coder p95 = 9.5 min, max = 18 min on crm_v1 build. Issues are
  being emitted at feature-size granularity. **Now item 4** of
  this roadmap.
