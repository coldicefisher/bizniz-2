# Bizniz Roadmap — 2026-05-15 onward

Locked in as of 2026-05-15 after the CRM v1 build surfaced enough
gaps to warrant explicit sequencing. **Work the items in order** —
each one's value compounds on the prior items.

Item 5 (agent error-path audit) inserted 2026-05-16 after CRM v1 M5
crashed twice from defensive-handling gaps; items 6-11 renumbered.

Items 12-13 (Immune system + Brain) appended 2026-05-16 to close the
roadmap: after the build-completion cycle is wired (12), bizniz can
evolve itself via bounded A/B testing (13).

## The 13-item plan

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

### 4. Granular issue decomposition via Decomposer agent ✅ v1 SHIPPED

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

**v1 shipped 2026-05-15** across three commits:

1. ✅ ``ae3883a`` — `bizniz/decomposer/` package (Decomposer agent
   + UnitOfWork/DecompositionResult types + prompts + 15 tests).
2. ✅ ``9f8a652`` — MilestoneCodeDispatcher wiring: optional
   ``decomposer_factory`` parameter, ``_decompose_issues`` helper,
   ``_unit_to_coder_issue`` shim. Defensive fallback when
   Decomposer fails — original issue dispatches as-is. 14 tests.
3. ✅ ``0e6fe0d`` — v2_build.py Decomposer factory wiring.
   Initially opt-in via ``--decompose``; **flipped to always-on
   default** same day per user direction (decomposition is the
   right architecture, no reason to gate it). Opt-out via
   ``v2_build --no-decompose`` for A/B comparison runs.

**Still pending** (Done-when criteria 4-5):

4. **Validation against live data**: run a fresh CRM-class build
   with ``--decompose`` and confirm p95 Coder-per-unit < 180s,
   p50 ~ 90s.
5. Refactor extractions (item 5) consume one unit per commit
   cleanly — pending item 5 implementation.

**v1 scope cuts (still deferred to follow-ups):**

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

### 5. Agent error-path audit — button up failure handling across all agents

Two crashes in the same week exposed a class of bugs: an agent
encounters or emits something unexpected, a strict validator
catches it, the exception propagates up unhandled, and the entire
pipeline halts mid-build. Both were single-line fixes once
identified:

- **CRM v1 M5 (commit `9258835`)** — `ProjectDB.mark_finished`
  raised `OperationalError: readonly database` from a stale sqlite
  connection. Fix: `_RetryingConnection` wrapper retries once with
  a reconnect. Halted a 27h build twice before the fix.
- **CRM v1 M5 repair iter 1 (commit `f24b5d7`)** —
  `ServicePlanner.repair` LLM emitted `BA-fix1-3 depends_on=
  ['BA-fix1-2']` without emitting `BA-fix1-2`. Fix:
  `_repair_dep_targets` drops bad edges with a warning instead of
  raising. Halted M5 just after frontend implement passed 44/44.

**The philosophy already in the codebase** (see
`_validate_files_non_empty` at `service_planner/agent.py:266`):

> Repair iterations are a side-channel. Losing one fix-issue is
> better than crashing the milestone.

Same wisdom applies broadly. Greenfield primary-path agents
(Planner, Architect, Provisioner, greenfield ServicePlanner) should
stay strict — failures there indicate real defects worth surfacing.
Side-channel agents (repair-mode ServicePlanner, integration
debugger, UX fix dispatch, smoke recovery, anything that runs after
the primary path has already succeeded once) should be lenient by
default — drop bad inputs, log a warning, keep the milestone moving.

**The audit:**

Per agent, classify every `raise` in the call path:

| Category | Action |
|---|---|
| Truly fatal (config invalid, contract violation in primary path) | Keep raising |
| LLM-emitted bad data in side-channel (repair, integration) | Drop / repair / log + continue |
| Transient infrastructure (rate limit, readonly DB, network) | Retry-with-backoff, then surface |
| Empty/missing optional field | Auto-fill default + log |

**Agents to walk:**

- [ ] `ServicePlanner` — partial (just shipped `_repair_dep_targets`,
      `_validate_files_non_empty`; still strict on cycles + duplicate
      IDs + empty plans in repair mode — review whether cycles
      should drop edges too)
- [ ] `Decomposer` — shipped 2026-05-15, no live failure modes yet
      but the same hallucination class applies (unit_id collisions,
      unknown depends_on references)
- [ ] `Coder` / `ClaudeCliCoder` — `TerminalActionRejected` already
      stalls instead of erroring (good); audit the rest
- [ ] `Tester` — same pattern as Coder
- [ ] `QuickDebugger` / `AgenticDebugger` — these run after
      something has already broken; their own crashes are the
      worst-case escalation
- [ ] `QualityEngineer.enrich` + `QualityEngineer.review` — both
      now load-bearing for the confidence gate (item 1)
- [ ] `CodeReviewer` — same
- [ ] `ProUXDesigner` — UX fix dispatch is a side-channel by
      definition; failures shouldn't tank the milestone
- [ ] `HTTPApiTester` / `WebUITester` / `WorkerTester`
- [ ] `SmokeRecovery` — partial (the try/except wrapper landed with
      the milestone.name fix); audit complete coverage
- [ ] `Refactorer` — audit at the same time it lands in item 6
- [ ] `ProjectDB` — partial (readonly retry shipped); audit other
      OperationalErrors
- [ ] `ProjectGit` — already best-effort by design (item 3 ships
      this); verify it really is no-throw

**Tests required for each lenient path:**

Mirror the `test_repair_drops_unknown_dep_instead_of_raising`
pattern: deliberately inject the bad input, assert the agent
returns sensibly with a warning logged. Lenient paths without
tests rot — a future "make it strict again" refactor silently
removes the leniency.

**Done when:**

1. Every `raise` in an agent has a known classification
   (fatal/lenient/transient/auto-fill)
2. Every lenient path has a regression test pinning the leniency
3. A `docs/agent_error_audit.md` index lists each agent + its
   error-path matrix (for future agents to follow the pattern)

**Why before Refactorer (was item 5):** Auditing existing agents
sets the pattern that Refactorer (and every future agent) inherits.
Cheaper than auditing N+1 agents later. Also a prerequisite for
items 10-11 (perf baselines on Claude + Gemini) — baseline numbers
mean nothing if the pipeline halts mid-run on a hallucinated dep.

### 6. Refactorer agent — dedupe + move to shared core

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

### 7. Tests / debugging after refactoring

Refactors can break things. After (5) runs, drive a focused
test+repair loop:
- Run every service's test suite
- On failure, dispatch the AgenticDebugger
- Roll back via git if convergence fails

**Done when:** refactorer-induced regressions are caught + fixed
automatically, not by humans noticing later.

### 8. Human documentation system

Agents write semantic documentation for the generated project:
- README.md per service (what it does, how to run it)
- API reference (auto-generated from OpenAPI + narrative)
- Architecture overview (services + interactions)
- "How to extend" guides per skeleton-shipped extension point

**Why this order:** the docs need to describe the *post-refactor*
shape, not the pre-refactor shape. Documenting twice is waste.

### 9. Detailed diagnostic + performance logging

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

### 10. Performance test on Claude

Build 3-5 reference projects (CRM, blog, e-commerce mini, ...) on
Claude with the full instrumentation from (8). Establish baselines:
- Wall clock per milestone
- Token cost per service per milestone
- Repair iterations needed
- UX score per route
- Refactor extractions per multi-service project

**Why on Claude first:** $0 marginal on Max plan lets us iterate
without budget pressure during baseline-finding.

### 11. Baseline on Gemini

Run the same reference projects on Gemini, compare against (9)'s
Claude baselines:
- Where does Gemini close the gap?
- Where does it widen?
- What prompt / agent tweaks move the needle?

**Goal:** durable architecture comparison, not a one-off run. Drives
where to invest next.

### 12. Immune system — the build-completion cycle

After items 6 (Refactorer), 7 (post-refactor tests/debug), and 8
(docs) ship individually, wire them into a single canonical
"build-completion" sequence at the end of every milestone and at
the end of the project:

```
Refactor → Full suite (unit + integration + e2e) → Document
```

Each step is a gate. Refactor produces clean code; the full test
suite catches refactor-induced regressions; docs reflect the final
post-refactor shape. **A milestone isn't DONE until all three pass.**

**Why an explicit item separate from 6/7/8:** Items 6-8 individually
ship the *components*. Item 12 ships the *cycle* — the wiring that
guarantees they always run together in order, with hard gates
between them, so we can't ship "refactored but undocumented" or
"refactored but tests didn't run."

**Evolve mode** — same components, different order:

```
Write tests → Refactor → Document → Work tickets/bugs (regular cycle)
```

For maintenance on an existing project: tests come first (write the
test that pins the desired behavior, THEN refactor to satisfy it,
THEN document). Tickets/bugs run through the standard pipeline as
today.

**Done when:** Both orderings selectable via a flag (e.g.,
``v2_build --mode build`` vs ``--mode evolve``). Each gates the
milestone; failure halts cleanly with structured ``RefactorReport``
/ ``TestReport`` / ``DocReport`` artifacts so the operator (or the
brain in item 13) can act on them.

### 13. Brain — self-evolving A/B testing

Once item 12 ships, bizniz has the *immune system* (refactor +
test + document) keeping it healthy. Item 13 adds the *brain* —
bizniz evolving itself against a reference problem until it stops
improving.

**The loop:**

1. Pick a **reference problem** (small, fast — Recipe Box or a
   subset of CRM). One that runs in well under an hour.
2. Establish a **baseline** via `perf_log` against the reference.
3. Pick a **knob to tweak**: which agent's prompt, which confidence
   threshold, which iteration cap, which model tier. The tweaks
   start small + bounded.
4. Build a **variant** with the tweak applied (in a sandbox branch).
5. Run the reference problem on the variant. Compare via
   ``perf_log --compare baseline.log variant.log``.
6. **Promote** the variant if it's better on a defined metric set
   (faster wall-clock + same-or-better confidence + same-or-better
   pass rate); revert if not.
7. **Loop** until ``N`` consecutive iterations produced no
   improvement (default ``N=3``). Then stop, surface the change
   set, and let the operator decide whether to merge into main.

**Critical safety properties:**

- Operator NEVER auto-merges to main. Brain produces PR-ready
  branches with measurements; humans review and approve.
- Brain operates in a sandboxed clone of the project root so
  experiments can't corrupt production state.
- Stop condition (``N`` iterations no improvement) is mandatory —
  open-ended loops are forbidden. If improvement plateaus, the
  brain STOPS and writes a summary, doesn't keep trying.
- Brain's tweaks come from a **bounded knob set** declared in
  config — not arbitrary code edits. Knobs are things like:
  - prompt-template versions (each agent ships ``v1``, ``v2``, ...
    prompts; brain picks one)
  - confidence band thresholds (item 1's 0.4/0.6 boundaries)
  - iteration caps (max_repair_iters, max_ux_iters)
  - model tier for a given agent
  - decomposer on/off, unit-of-work sizing

**The metric set** (defines "better"):

- Wall-clock per milestone (lower is better)
- Total tokens / API cost per milestone
- First-try pass rate (issues passing without repair iter)
- UX score (mean across views)
- Final test pass rate
- Confidence score across enrich + review

A variant wins only if it improves at least one metric without
regressing any other by more than 5%.

**Done when:** Running ``bizniz evolve --target recipe_box
--knob ux.fix_iteration_cap --max-iters 10`` produces a structured
``EvolveReport`` showing the tested values, the metrics per
iteration, and the recommended winner. Operator reviews + merges
the prompt/config change.

**Why this is the right shape (vs continuous self-modification):**

- Bounded knob set = can't break the agent that's modifying
- Reference problem = fast feedback (vs full-build evolution)
- Stop-after-N-without-improvement = avoid infinite loops
- Human-merge gate = catches regressions an automated metric set
  doesn't measure (e.g., code quality the metric set can't see)

This is the SAFE form of "self-building bizniz." Open-ended self-
prompt-modification is explicitly out of scope — too risky.

## Order rationale

- **1 first** because it's the cheapest quality lever — preventing
  burnt cycles on ambiguous specs costs basically nothing to wire
  and earns every downstream item more reliable inputs.
- **2 next** because UX is the most user-visible quality bar — the
  thing buyers see — and Storybook is the right shape.
- **3 → 4 → 5 → 6 → 7** is the refactor-safety stack. Can't do
  refactor (6) without version control (3); item 4 (granular issues)
  means each refactor extraction is one atomic commit; item 5
  (error-path audit) hardens every existing agent before adding
  another one in item 6; item 7 catches what 6 misses.
- **8 after 6** so docs reflect the refactored shape, not the
  pre-refactor sprawl.
- **9 before 10** so the baseline data exists in structured form.
- **10 before 11** so we have Claude numbers to compare Gemini against.
- **12 after 6-7-8** — items 6/7/8 ship the *components*; item 12
  wires them into a single canonical cycle with hard gates between.
  The cycle can't exist before the components do.
- **13 last** — the brain needs the immune system (item 12) to
  catch regressions AND structured metrics from item 9 to know
  what "better" means. Both must ship first.

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
