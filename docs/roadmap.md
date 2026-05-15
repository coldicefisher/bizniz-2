# Bizniz Roadmap — 2026-05-15 onward

Locked in as of 2026-05-15 after the CRM v1 build surfaced enough
gaps to warrant explicit sequencing. **Work the items in order** —
each one's value compounds on the prior items.

## The 9-item plan

### 1. Confidence signals — load-bearing or drop the pretense

QE's enrich prompt language says "confidence < 0.6 means Engineer
should ask follow-up questions" but **nothing in code acts on the
score**. Same for CodeReviewer.review.confidence (logged, not
gating). Today only the UX vision score is load-bearing.

Make `QualityEngineer.enrich.confidence` load-bearing as the
reference impl, then audit + retrofit the rest. Three bands:

- **≥ 0.6**: implement normally (current path)
- **0.4-0.6**: one re-enrich pass with augmented prompt ("list your
  ambiguities and either resolve them or write TODOs the Engineer
  surfaces")
- **< 0.4**: halt at a new `enrich_low_confidence` soft gate
  (`--auto` pushes through with a warning)

Full ticket: `docs/backlog/confidence_signals.md`. Includes the
meta-pattern audit (CodeReviewer, Coder, Tester, Architect, Planner
all need this shape eventually).

**Why first:** every later item benefits when QE flags ambiguous
milestones BEFORE 30 minutes of implement/repair burn through on a
spec the model didn't trust to begin with. Cheapest possible
quality lever.

**Done when:** ambiguous milestones either get a re-enrich pass or
halt for review. CodeReviewer + Coder confidence audits scheduled
for item 7 (perf logging) to ride the same instrumentation.

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

### 3. Add version control

Per-project git ops baked into the pipeline:
- Initialize `git` in `~/bizniz_projects/<slug>/` on first run
- Commit per phase (planner → architect → provisioner → per
  milestone → per repair iter → integration → UX)
- Branch per milestone so a failure is reversible without losing
  prior work
- Tag DONE state at each milestone boundary

**Why before refactorer:** the refactorer (item 4) MUST run on a
commit-tracked codebase or we can't safely roll back a bad refactor.

### 4. Refactorer agent — dedupe + move to shared core

We have a v1 Refactorer (single Claude CLI session at the project
root). It works minimum-viable but doesn't:
- Move API code to shared core libraries when business logic
  duplicates across services
- Detect cross-service abstraction opportunities
- Coordinate with version control to commit each extraction
  separately

**Done when:** a multi-service project's business logic lives in
`shared/<lang>/` libraries, consumer services import them, tests
pass, each extraction is its own commit.

### 5. Tests / debugging after refactoring

Refactors can break things. After (4) runs, drive a focused
test+repair loop:
- Run every service's test suite
- On failure, dispatch the AgenticDebugger
- Roll back via git if convergence fails

**Done when:** refactorer-induced regressions are caught + fixed
automatically, not by humans noticing later.

### 6. Human documentation system

Agents write semantic documentation for the generated project:
- README.md per service (what it does, how to run it)
- API reference (auto-generated from OpenAPI + narrative)
- Architecture overview (services + interactions)
- "How to extend" guides per skeleton-shipped extension point

**Why this order:** the docs need to describe the *post-refactor*
shape, not the pre-refactor shape. Documenting twice is waste.

### 7. Detailed diagnostic + performance logging

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

### 8. Performance test on Claude

Build 3-5 reference projects (CRM, blog, e-commerce mini, ...) on
Claude with the full instrumentation from (7). Establish baselines:
- Wall clock per milestone
- Token cost per service per milestone
- Repair iterations needed
- UX score per route
- Refactor extractions per multi-service project

**Why on Claude first:** $0 marginal on Max plan lets us iterate
without budget pressure during baseline-finding.

### 9. Baseline on Gemini

Run the same reference projects on Gemini, compare against (8)'s
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
- **3 → 4 → 5** is the refactor-safety stack. Can't do 4 without 3;
  5 catches what 4 misses.
- **6 after 4** so docs reflect the refactored shape, not the
  pre-refactor sprawl.
- **7 before 8** so the baseline data exists in structured form.
- **8 before 9** so we have Claude numbers to compare Gemini against.

## What's NOT in this plan (deferred)

- Angular skeletons (skeleton-angular, teams, saas) get Storybook
  scaffolding only AFTER item 2 proves the React loop end-to-end.
- Full escalation on smoke gate (replace hard-fail with cheap-tier
  AgenticDebugger one-shot) — ALREADY SHIPPED in commit `29e5ea9`
  via `SmokeRecovery`. Future work: extend to multi-tier escalation
  inside roadmap item 5 if one-shot isn't enough on real failures.
- Production-mode Dockerfile variants (no `--reload`, Alembic
  migrations) — deferred until the dev-mode loop is stable.

## Feedback baked in this session

- On smoke failures, prefer full agent escalation (try to recover
  the container) rather than hard-halt at the gate. **Shipped
  2026-05-15 (`29e5ea9`)** — one-shot `SmokeRecovery` agent runs
  before hard-halt. Multi-tier escalation deferred to item 5.
- Agent self-rated confidence fields should be load-bearing or
  removed from prompts. **Now item 1** of this roadmap.
