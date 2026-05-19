# v3 Pipeline Spec — A/B test plan for the next architecture

**Status:** spec ready for implementation. Phase 1 + Phase 2a both validated
2026-05-18. The proposed architecture has anchor data and green-lights on
its load-bearing claims.
**Anchor data:** recipe_v3 M1 baselines + perf-test runs collected 2026-05-18.

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
That tells us Haiku Coder is safe — but Haiku review-class agents collapse
under multi-iteration context pressure.

## What we've validated (anchor data)

| Phase | Status | Anchor | Result |
|---|---|---|---|
| Phase 1 — ServicePlanner seeded scaffold | ✓ VALIDATED | recipe_v3 M1 backend | 228s wall, 8/8 AST, 8/8 symbol, 8/8 coverage |
| Phase 1 — frontend (TypeScript) | ✓ VALIDATED (soft pass) | recipe_v3 M1 frontend | 111s wall, 11/11 coverage, 5 trivial tsc errors (prompt-refinable) |
| Phase 2a — Single-dispatch CoderAgent | ✓ VALIDATED | recipe_v3 M1 backend, 7 issues | **1m 55s** wall, 8/8 AST, 8/8 symbol, 8/8 coverage, zero NotImplementedError, no drift, 32 production-grade tests |

**The Phase 2a number is load-bearing.** It collapsed 7 issues' worth of work
(code + tests) into a single 1m 55s LLM call. Tonight's Opus M1 IMPLEMENT
phase did 12 issues sequentially in 1h 35m. That's a projected **~25× wall
reduction on the IMPLEMENT phase alone**.

## Architectural simplification — single CoderAgent (no TesterAgent)

**The original spec proposed parallel CoderAgent + TesterAgent.** Phase 2a's
validation showed a unified agent writes both code AND tests at production
quality in one dispatch — including algorithm-confusion defenses, meta-
consistency role-policy tests, and proper fixture setup. The parallel split
was justified for *wall-clock savings* (run both at once) and *contract
purity* (Tester blind to Coder's choices). Neither holds:

- **Wall savings:** unified agent already takes 1m 55s for 7 issues. Splitting
  to halve that saves ~30-60s — marginal.
- **Contract purity:** the seeded scaffold IS the contract. Both code and
  tests bind to it. The agent never "peeks at implementation choices" because
  both filled outputs come from the same prompt that only saw the scaffold.

Dropping TesterAgent removes:
- 1 agent role to design + maintain
- 1 prompt to keep aligned
- The "contract reconciler" between Coder and Tester outputs (no drift can
  occur between agents because there's only one agent)
- The "Tester refresh" agent (no drift to refresh from)
- The parallel-composition orchestration complexity

**The TesterAgent and reconciler stay on the table as future optimizations** —
revisit when (a) single-agent dispatch hits context limits, OR (b) wall
gets dominated by a single-call output time. Neither is a current concern.

## What changes — pipeline diff

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

                                           [Gate: AST + symbol +              ← NEW
                                            hallucination check on seeds]      (Haiku for hallucination)

    QualityEngineer.enrich (Opus)          QualityEngineer.enrich (Opus)       [SAME]

    Coder × N issues                       SINGLE CoderAgent                   [BIG CHANGE]
      tool loop, runs tests inline           (one call, all N issues,           (N → 1, fills code
                                              fills code + tests against        + tests, no inline
                                              the seeded contract,              test execution)
                                              structured output JSON)

                                           [Gate: AST + symbol +              ← NEW
                                            bodies-filled +                    (deterministic)
                                            no-drift check]

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
    HTTPApiTester (Opus, per svc)            (tests already exist from         (CoderAgent's output
    WebUITester (Opus, per svc)               CoderAgent — no rewrite)          covers integration too)
    pytest                                   pytest
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
| **Static check batch** | n/a | **NEW** | deterministic | mypy/ruff/tsc/pytest --collect-only in parallel → normalized findings list |
| **CoderAgent (v3)** | Coder × N (per issue) | **N → 1** | Haiku → Opus escalation | Single call per milestone per service. Writes BOTH code AND tests. Pure structured output (no tool loop). Reads seeded scaffold + issue specs. Phase 2a validated. |
| **ServicePlanner** | issues only | **+ seeded scaffold** | Opus | Same role, extended output schema. Now produces concrete signatures + imports + types + body stubs alongside issue specs. Phase 1 validated. |
| **AgenticDebugger** | one signal at a time | **batch-fix on unified findings** | Haiku triage → Opus workhorse → Opus final | Same tool surface (Read/Edit/Write/Bash/Glob/Grep + MCP), ProgressTracker bounds, batch-fix prompt. |
| **AuthOperator.code_examples** | Opus | tier move only | **Haiku** | Sample-code generator, low stakes |
| ~~**TesterAgent**~~ | — | **NOT NEEDED** | — | Dropped from spec. CoderAgent handles both. See "Architectural simplification" above. |
| ~~**Contract reconciler**~~ | — | **NOT NEEDED** | — | Dropped — no drift between agents to reconcile (only one agent writes everything). |
| ~~**Tester refresh agent**~~ | — | **NOT NEEDED** | — | Dropped — no Tester to refresh. |
| **Decomposer** | opt-in (default off) | unchanged | Opus (when on) | Stays opt-in |
| Planner, Architect, AuthPlanner, QualityEngineer (.enrich + .review), CodeReviewer, Refactorer, ProUXDesigner, IntegrationDebugger workhorse | unchanged | **SAME** | Opus | No role or tier changes |

## Structural deltas

| Property | A (today) | B (proposed) |
|---|---|---|
| Coder calls per milestone | N (5-24+) | **1 per service** |
| Tester calls per milestone | M (1-3, per-service) | **0 (CoderAgent does both)** |
| Per-issue inline test execution by Coder | yes | **no** (tests run in dedicated review-unit phase) |
| Reviews (QA/test/CR) | sequential | **parallel 4-way fan-out** |
| Debugger input | one stream (test output) | **all signals unified** |
| Review/repair iteration loops | 3 (QA, test, CR separate) | **1** (unified review unit) |
| Initial contract for milestone | issue specs only | **seeded scaffold (concrete signatures) + specs** |
| Contract drift between agents | implicit (debugger figures out) | **N/A (single agent)** |
| Hallucination gating | per-Coder-output only | **after ServicePlanner seed + after CoderAgent output** |
| Deterministic pre-flight | none | **mypy/ruff/tsc/collect-only batch** |
| Debugger bounds | per-tier attempts × repair_attempts | **ProgressTracker: stall_threshold=5, resets on findings drop; per-iteration tool turn budget generous** |
| Decomposer | opt-in (off by default) | unchanged (opt-in) |

## Wall projection — M1 anchor data → B projection

**Anchor (A, real, captured 2026-05-18):**
- Plan + Architect + Provision + Auth: ~5 min
- ServicePlanner × 2 services: 2m 47s
- IMPLEMENT (24 Coder dispatches): **1h 35m**
- Review/repair (2 iters, 86→9→0 defects): **49 min**
- Integration phase API + Web (+3 ClaudeCliDebugger calls, 14m 14s): **21 min**
- Post-integration smoke + final tester: ~1 min
- UX phase (ProUXDesigner full cycle): **32m 14s**
- Refactor + Document recovery: ~3 min
- **Total M1 wall: 3h 29m 38s**

**Phase 2a anchor (B, real):**
- CoderAgentV3 on 7 issues + 8 seeded files: **1m 55s** total wall
- Output: 32 test functions + 1 route + 1 schema, all bodies filled, all
  symbols resolve, no contract drift

**Projection (B, M1):**

| Phase | A (real) | B (projected) | Anchor source |
|---|---|---|---|
| Plan/Architect/Provision/Auth | 5m | 5m | unchanged |
| ServicePlanner + seeded scaffold (× 2 svcs) | 3m | 5-6m | Phase 1: 228s for backend |
| Seed validation gates (Haiku hallucination + AST/symbol) | n/a | 30-60s | deterministic checks + 1 Haiku call |
| CoderAgent fills code + tests (per service) | 1h 35m | **~3-5m** | Phase 2a: 1m 55s for 7 issues; project 3-5m for 12+ |
| Output gates (AST + symbol + bodies + drift) | n/a | 30s | deterministic |
| Static checks batch (mypy/ruff/tsc/collect-only) | n/a | 15s | deterministic, parallel |
| Parallel review unit (pytest + QA + CR) | 1h 35m IMPLEMENT + 49m repair | **5-10m × 1-3 iters** | parallel fan-out, batch-fix |
| Integration phase | 21m | 15-20m | tests already exist from CoderAgent |
| UX phase | 32m | 32m | unchanged |
| Refactor + final tester | 3m | 3m | unchanged |
| **Total M1 projected** | **3h 29m** | **~45-60 min** | |

**~3.5-4× M1 wall reduction projected.** Extrapolated to 5 milestones:
17h → 4-5h. Inside the "ship a recipe site overnight" range. Further wall
savings would need attacking the UX phase (32m fixed cost) or the
integration phase (15-20m fixed cost) — separate efforts.

## Risks + open questions

| Risk | Mitigation |
|---|---|
| Single CoderAgent hits context limit on big milestones | Haiku context is 200K, generally enough. Escalate to Opus tier on context error. Worst case: reintroduce per-service or per-layer split. |
| CoderAgent generates ambiguous tests under contract drift | The seed IS the contract. AST + symbol gate after output catches drift. |
| Batch-fix debugger fixes A which regresses B | Re-run review unit catches it. ProgressTracker handles stall via tier escalation. |
| Unified findings report grows huge under many failures | Severity-prioritize + trim before sending. Static check findings are short. |
| Seeded scaffold from ServicePlanner gets the contract wrong | Hallucination check + AST/symbol gate catches it. Worst case: re-prompt ServicePlanner with the failures. Phase 1 already showed clean output. |
| Haiku Coder confabulates on edge cases not covered by BE-006 / BA-fix2-2 / Phase 2a | Tier escalation to Opus on stall (already in `coder_models` config). |
| QualityEngineer fan-out + multiple iterations could hit Haiku-like context limits on Opus | Trim previous-iteration findings out of subsequent prompts. Pin QE on Opus (200K context). |

## Test plan — what we A/B test

**A**: current pipeline (`bizniz.yaml` as committed today, no Decomposer)
**B**: new pipeline as specced above

**Comparison fixture**: recipe_v3 problem statement (`examples/prompts/recipe_box.txt`),
clean greenfield builds.

### Phase 1 — ServicePlanner seeded scaffold (DONE ✓)
- Anchor: recipe_v3 M1 backend + frontend
- Pass criteria: wall ≤ 6 min, AST ≥ 100%, symbol ≥ 80%, coverage = 100%
- Result: backend PASS (228s, 8/8/8/8); frontend PASS soft (111s, 11/11 coverage, 5 trivial tsc errors are prompt-refinable)

### Phase 2a — CoderAgent single-dispatch (DONE ✓)
- Anchor: recipe_v3 M1 backend, 7 issues + 8 seeded files
- Pass criteria: wall ≤ 15 min, AST = 100%, symbol ≥ 80%, coverage = 100%, bodies filled = 100%, no drift
- Result: **PASS (114.8s, all 6 gates green, production-grade content)**

### Phase 2c — Larger-scope validation (NEXT)
- Anchor: recipe_v2 M3 (tags+search+filter, 12+ backend issues) or recipe_v3 M2
- Pass criteria: same as 2a but at production scope (12-24 issues)
- Goal: confirm Phase 2a scales beyond a 7-issue milestone

### Phase 3 — Parallel review unit (after 2c)
- Build the fan-out: static + pytest + QE + CR in parallel
- Compare unified-findings batch-fix debugger vs today's sequential debugger
- Measure: wall, findings convergence rate, iteration count to clean

### Phase 4 — Full M1 A/B (final validation)
- Run recipe_v3 M1 under A (today's pipeline) + B (new pipeline) end-to-end
- Measure full M1 wall + per-phase breakdown

### Phase 5 — Deterministic batch checks (post-phase-4)
- Add mypy/ruff/tsc/collect-only to the static check batch
- Re-run M1 under B + extended static checks
- Measure delta on iteration count + debugger wall

## Implementation roadmap

(Revised after Phase 1 + 2a validations + TesterAgent removal.)

| Phase | Components | Est. hours | Sequenced after |
|---|---|---|---|
| 1 | ServicePlanner seeded-scaffold output | DONE | — |
| 2 | Promote `service_planner/scaffolded.py` → `agent.py`; promote schema + prompt | 1-2 | (1) |
| 3 | Hallucination-check agent | 2-3 | — |
| 4 | Output gates: AST + symbol + bodies + drift checks wrapped as a gate function | 2 | (2) |
| 5 | CoderAgent (`coder/agent_v3.py`) — already built, promote to production | 1-2 | (2) |
| 6 | MilestoneCodeDispatcher: single dispatch per service (not per issue) | 3-4 | (5) |
| 7 | Static check batch (mypy/ruff/tsc/collect-only as a parallel run) | 2-3 | — |
| 8 | Parallel review unit orchestration | 3-4 | (5, 7) |
| 9 | Unified findings aggregator | 2 | (8) |
| 10 | Batch-fix Agentic Debugger (prompt + ProgressTracker integration) | 4-5 | (9) |
| 11 | MilestoneLoop refactor to drive the new units | 3-4 | (10) |
| 12 | Tests + harness work | 4-5 | (continuous) |
| **Total** | | **~25-35 hours** | |

(Down from the original 32-42 hours by removing TesterAgent, contract
reconciler, and Tester-refresh agent. Phase 1 already shipped.)

## Open questions for next session

1. **Coder context window strategy at scale:** when CoderAgentV3's prompt exceeds Haiku's effective context, do we escalate or split? Recommend: try Opus escalation first; fall back to per-service split only if Opus also OOMs.
2. **Static check tooling:** mypy is slow on big codebases. Consider pyright for the type check leg. Bench on recipe_v3 to decide.
3. **Decomposer:** keep opt-in indefinitely, or remove entirely? Two-fixture data already says it's net-negative. Decision deferred until B ships.
4. **When to re-evaluate TesterAgent split:** trigger conditions = single-dispatch wall exceeds 10 min OR output size exceeds 50KB OR context exceeds 100K tokens.

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
