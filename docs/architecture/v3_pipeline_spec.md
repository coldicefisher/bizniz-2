# v3 Pipeline Spec — A/B test plan for the next architecture

**Status:** spec for A/B validation. Implementation gated on this run's data.
**Anchor data:** recipe_v3 M1 baselines collected 2026-05-18.

## Why we're changing

Recipe-class builds today take 4+ hours on Opus and don't even reach M2.
Recipe_v3 M1 Opus baseline (real data captured tonight):

```
Wall clock:    3h 29m 38s  (M1 alone — single milestone)
Coder dispatch:   1h 35m  (45%, dominant time sink)
UX phase:           32m   (15%)
Review/repair:      49m   (24%)
Integration:        21m   (10%)
Other:              ~10m
```

At 3.5h per milestone × 5 milestones = ~17h end-to-end. The 4-hour-recipe-site
problem is structural — not a single-bug optimization away.

The Haiku-everywhere variant attempt failed at review/repair iter 2 with
`Prompt is too long` (context limit on QE.review's many-files-inlined prompt).
That tells us Haiku Coder/Tester is safe — but Haiku review-class agents
collapse under multi-iteration context pressure.

## What changes — pipeline diff

### Top-level shape

```
A (today)                              B (proposed)
─────────────────────────────────      ─────────────────────────────────
Planner (Opus)                         Planner (Opus)                          [SAME]
Architect (Opus)                       Architect (Opus)                        [SAME]
Provisioner (deterministic)            Provisioner (deterministic)             [SAME]
AuthPlanner (Opus)                     AuthPlanner (Opus)                      [SAME]
AuthOperator.code_examples (Opus)      AuthOperator.code_examples (Haiku)      [TIER ↓]

[per milestone]                        [per milestone]
  [per service]                          [per service]
    ServicePlanner (Opus)                  ServicePlanner (Opus)               [EXTENDED]
      emits N issue specs                    emits N issue specs +
                                             SEEDED SCAFFOLD (signatures,
                                             imports, types, but no bodies)

                                           [Gate: AST + symbol + hallucination ← NEW
                                            check on seeds] (Haiku check)

    QualityEngineer.enrich (Opus)          QualityEngineer.enrich (Opus)       [SAME]

    Coder × N issues                       PARALLEL on seeded contract:        [BIG CHANGE]
      tool loop, runs tests inline           ├─ CoderAgent (single call         (N → 1, parallel,
                                             │  for all N issues, no             contract-bound, no
                                             │  inline tests)                    inline tests)
                                             └─ TesterAgent (single call
                                                for all N tests, parallel
                                                with CoderAgent)

                                           [Contract reconciler:               ← NEW (deterministic)
                                            AST extract symbols → diff
                                            vs seed → symbol-validator gate]

                                           [Tester refresh — surgical          ← NEW (Haiku)
                                            only for tests referencing
                                            drifted names]

  REVIEW/REPAIR (sequential):              REVIEW UNIT (PARALLEL fan-out):     [BIG CHANGE]
    QualityEngineer.review                   ┌─ Static checks: mypy/ruff/      (sequential → parallel
              ↓                              │  tsc/pytest --collect-only       3 LLM passes → 4-way
    CodeReviewer                             │  (deterministic)                 fan-out → unified
              ↓                              ├─ Pytest execution                findings)
    if defects:                              │  (deterministic)
      ServicePlanner.repair                  ├─ QualityEngineer.review (Opus)
      Coder × N fix-issues                   └─ CodeReviewer (Opus)
      loop until approved                              ↓
                                             Aggregate → unified findings
                                                       ↓
                                             [if zero] → integration
                                             [if findings] → Batch-fix
                                               Agentic Debugger
                                               (Haiku triage → Opus workhorse,
                                                full tool surface,
                                                ProgressTracker-bounded:
                                                stall_threshold=5,
                                                resets on findings_count drop)
                                                       ↓
                                             Loop back to review unit
                                             until clean OR stall escalation

  Integration phase:                       Integration phase:                  [SAME shape]
    HTTPApiTester (Opus, per svc)            (tests already exist from         (TesterAgent's
    WebUITester (Opus, per svc)               TesterAgent — no rewrite)         output covers
    pytest                                   pytest                              integration too)
    on fail: IntegrationDebugger             on fail: same batch-fix debugger
                                              with full failure report

  UX_REVIEW (if frontend)                  UX_REVIEW (if frontend)              [SAME]
  REFACTOR (milestone boundary)            REFACTOR (milestone boundary)        [SAME]
  M{N} DONE                                M{N} DONE
```

### Agents — added / changed / removed

| Agent | A status | B status | Tier in B | Notes |
|---|---|---|---|---|
| **Hallucination-check agent** | n/a | **NEW** | Haiku | Pattern detection (catch-all except, swallowed exceptions, fake module refs) on seeded scaffold + each LLM output |
| **Contract reconciler** | n/a | **NEW** | deterministic | AST walk → public symbol table → diff vs seed |
| **Static check batch** | n/a | **NEW** | deterministic | mypy/ruff/tsc/pytest --collect-only in parallel → normalized findings list |
| **Tester refresh agent** | n/a | **NEW** | Haiku | Surgical edit pass only for tests referencing drifted symbols |
| **CoderAgent** | Coder × N | **N → 1** | Haiku → Opus escalation | Single call per milestone, all issues. No inline tests. Reads seeded scaffold + issue specs. |
| **TesterAgent** | per-service in integration | **N → 1, parallel** | Haiku → Opus escalation | Single call per milestone, all tests. Reads seeded scaffold (contract) + issue specs. Runs parallel with CoderAgent. |
| **ServicePlanner** | issues only | **+ seeded scaffold** | Opus | Same role, extended output schema. Now produces concrete signatures + imports + types alongside issue specs. |
| **AgenticDebugger** | one signal at a time | **batch-fix on unified findings** | Haiku triage → Opus workhorse → Opus final | Same tool surface (Read/Edit/Write/Bash/Glob/Grep + MCP), ProgressTracker bounds, batch-fix prompt. |
| **AuthOperator.code_examples** | Opus | tier move only | **Haiku** | Sample-code generator, low stakes |
| **Decomposer** | opt-in (default off) | unchanged | Opus (when on) | Stays opt-in |
| Planner, Architect, AuthPlanner, QualityEngineer (.enrich + .review), CodeReviewer, Refactorer, ProUXDesigner, IntegrationDebugger workhorse | unchanged | **SAME** | Opus | No role or tier changes |

## Structural deltas

| Property | A (today) | B (proposed) |
|---|---|---|
| Coder calls per milestone | N (5-24+) | **1** |
| Tester calls per milestone | M (1-3, per-service) | **1** |
| Parallel Coder + Tester? | no | **yes** |
| Per-issue inline test execution by Coder | yes | **no** (dedicated test phase) |
| Reviews (QA/test/CR) | sequential | **parallel 4-way fan-out** |
| Debugger input | one stream (test output) | **all signals unified** |
| Review/repair iteration loops | 3 (QA, test, CR separate) | **1** (unified review unit) |
| Initial contract for Tester | issue specs only | **seeded scaffold (concrete signatures) + specs** |
| Contract drift handling | implicit (debugger figures out) | **explicit** (AST reconciler + Tester refresh) |
| Hallucination gating | per-Coder-output only | **also after ServicePlanner seed** |
| Deterministic pre-flight | none | **mypy/ruff/tsc/collect-only batch** |
| Debugger bounds | per-tier attempts × repair_attempts | **ProgressTracker: stall_threshold=5, resets on findings drop; per-iteration tool turn budget generous** |
| Decomposer | opt-in (off by default) | unchanged (opt-in) |

## Wall projection — M1 anchor data → B projection

**Anchor (A, real):**
- Plan + Architect + Provision + Auth: ~5 min
- ServicePlanner × 2 services: 2m 47s
- IMPLEMENT (24 Coder dispatches): **1h 35m**
- Review/repair (2 iters, 86→9→0 defects, 4 ServicePlanner.repair calls, ~24 Coder fix dispatches): **49 min**
- Integration phase API + Web (+3 ClaudeCliDebugger calls, 14m 14s): **21 min**
- Post-integration smoke + final tester: ~1 min
- UX phase (ProUXDesigner full cycle): **32m 14s**
- Refactor + Document recovery: ~3 min
- **Total M1 wall: 3h 29m 38s**

**Projection (B):**

| Phase | A (real) | B (projected) | Why |
|---|---|---|---|
| Plan/Architect/Provision/Auth | 5m | 5m | unchanged |
| ServicePlanner + seeded scaffold | 3m | 5-6m | bigger output (scaffold) |
| Seed validation gates | n/a | 1-2m | new (Haiku hallucination check + AST/symbol/import-smoke) |
| Coder + Tester parallel | 1h 35m + Tester-in-integration | **10-15m** | N→1 collapse, parallel fan-out |
| Contract reconciler + symbol gate | n/a | 30s | AST walk (deterministic) |
| Static checks batch | n/a | 15s | mypy/ruff/tsc/collect (parallel, deterministic) |
| Parallel review unit (pytest + QA + CR) | 1h 35m IMPLEMENT included + 49m repair | **5-10m × ~1-3 iter** | parallel fan-out, batch-fix debugger |
| Integration phase | 21m | 15-20m | tests already exist from TesterAgent |
| UX phase | 32m | 32m | unchanged |
| Refactor + final tester | 3m | 3m | unchanged |
| **Total M1 projected** | **3h 29m** | **~50-75 min** | |

**~2.5-3× M1 wall reduction projected.** Extrapolated to 5 milestones: 17h → 5-7h.
Still long, but inside the "ship a recipe site overnight" range. Further wall
savings would need attacking the UX phase (32m fixed cost) and the integration
phase (15-20m fixed cost).

## Risks + open questions

| Risk | Mitigation |
|---|---|
| Single CoderAgent hits context limit on big milestones | Haiku context is 200K, generally enough. Escalate to Opus tier on context error. Worst case: re-introduce per-service split (not per-issue). |
| Single TesterAgent generates ambiguous tests under contract drift | Tester refresh agent + symbol-validator gate catches drift before pytest. |
| Batch-fix debugger fixes A which regresses B | Re-run review unit catches it. ProgressTracker handles stall via tier escalation. |
| Unified findings report grows huge under many failures | Severity-prioritize + trim before sending. Static check findings are short. |
| Seeded scaffold from ServicePlanner gets the contract wrong | Hallucination check + AST/symbol gate catches it. Worst case: re-prompt ServicePlanner with the failures. |
| Haiku Coder confabulates on edge cases not covered by BE-006 / BA-fix2-2 | Tier escalation to Opus on stall (already in `coder_models` config). |
| QualityEngineer fan-out + multiple iterations could hit Haiku-like context limits on Opus | Trim previous-iteration findings out of subsequent prompts. Pin QE on Opus (200K context) — observed today it survives a 21-min repair iter at full context. |

## Test plan — what we A/B test

**A**: current pipeline (`bizniz.yaml` as committed today, no Decomposer)
**B**: new pipeline as specced above

**Comparison fixture**: recipe_v3 problem statement (`examples/prompts/recipe_box.txt`),
clean greenfield builds.

**Phases of validation:**

### Phase 1: Validate the seeded-scaffold idea (lightweight)
Microbenchmark: extend ServicePlanner output schema to include seeded scaffold.
Run on recipe_v3 M1 (using captured plan from tonight). Validate:
- Scaffold passes AST + symbol-validator + hallucination check
- ServicePlanner wall < 2× current (target +30-60s)
- Scaffold contains all symbols referenced by the issue specs

If this fails or is unstable: spec needs rework. If it passes: proceed.

### Phase 2: Validate parallel CoderAgent + TesterAgent on a single milestone
Build `single_agent_milestone` perf-test fixture. Replay recipe_v3 M1 plan + seeded scaffold.
- A: 24 sequential Coder calls (today's wall: 1h 35m)
- B: 1 CoderAgent + 1 TesterAgent in parallel

Measure: wall, AST pass rate, symbol-validator pass rate, structural pattern coverage.

### Phase 3: Validate parallel review unit + batch-fix debugger
With B's CoderAgent/TesterAgent outputs, run:
- A: sequential QE → CR → pytest → debugger (today's pattern)
- B: parallel static + pytest + QE + CR → aggregate → batch-fix debugger

Measure: wall, findings convergence rate, iteration count to clean.

### Phase 4: Full M1 A/B
Run recipe_v3 M1 under A (today's pipeline) + B (new pipeline) end-to-end.
Measure full M1 wall + per-phase breakdown.

### Phase 5: Test plan addition — deterministic batch checks
After phase 4: add mypy/ruff/tsc/pytest --collect-only to the static check
batch. Re-run M1 under B + extended static checks. Measure delta on iteration
count + debugger wall.

## Implementation roadmap (when greenlit)

| Phase | Components | Est. hours | Sequenced after |
|---|---|---|---|
| 1 | ServicePlanner seeded-scaffold output | 3-4 | — |
| 2 | Hallucination-check agent | 2-3 | — |
| 3 | Contract reconciler (AST + symbol diff) | 2 | (1) |
| 4 | CoderAgent (N-issue single dispatch) | 4-5 | (1) |
| 5 | TesterAgent (parallel with Coder, contract-bound) | 3-4 | (1) |
| 6 | Tester refresh agent | 2 | (3, 5) |
| 7 | Parallel review unit (static + pytest + QE + CR fan-out) | 3-4 | (4, 5) |
| 8 | Unified findings aggregator | 2 | (7) |
| 9 | Batch-fix Agentic Debugger (prompt + ProgressTracker integration) | 4-5 | (8) |
| 10 | MilestoneLoop refactor to drive the new units | 3-4 | (6, 9) |
| 11 | Tests + harness work | 4-5 | (continuous) |
| **Total** | | **~32-42 hours** | |

## Open questions for next session

1. **Coder context window strategy:** when CoderAgent's prompt exceeds Haiku's effective context, do we escalate or split? Recommend: try Opus escalation first; fall back to per-service split only if Opus also OOMs.
2. **Tester refresh trigger:** is "any symbol drift" the right trigger, or should we threshold (e.g., refresh only if ≥3 tests reference drifted names)?
3. **Static check tooling:** mypy is slow on big codebases. Consider pyright for the type check leg. Bench on recipe_v3 to decide.
4. **Decomposer:** keep opt-in indefinitely, or remove entirely? Two-fixture data already says it's net-negative. Decision deferred until B ships.

## What stays the same

- Top-level orchestration (Planner → Architect → Provisioner → Auth)
- All skeleton infrastructure (fastapi/react/angular/teams/postgres/redis/fusionauth)
- All deterministic tooling (port reservation registry, host/container port split, docker compose generation)
- AuthOperator core (just code_examples tier move)
- UX phase (ProUXDesigner)
- Refactor phase (v3 RefactorerAgent)
- Final tester + smoke phase
- Cost tracking + perf_log instrumentation
- Tier escalation infrastructure (already supports Haiku → Opus chains)
- 429 retry handling + port-conflict reservation registry
