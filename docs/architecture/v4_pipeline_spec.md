# v4 Pipeline Spec — Parallel issue execution + Coder/Tester merge

**Status:** spec — implementation not yet started.
**Anchor data:** recipe_v4_v31 M1 partial run, 2026-05-19. Killed
during repair iter 2 once architectural data was sufficient.
**Predecessor:** [[v3_pipeline_spec.md]] (v3 Stage A IMPLEMENT shipped,
v3.1 review/repair shipped).

## Why we're pivoting

v3.1 shipped today and validated the architectural premises it
combined. The data also surfaced the **actual remaining bottleneck**:

```
v3.1 M1 wall (recipe_v4_v31, halted mid-iter-2 at user request):
  Plan + Architect + Provision + Auth + QE enrich   ~5 min
  IMPLEMENT (Stage A)                              18 min
  Repair iter 1 — coder subprocesses               38 min   ← 86% of iter wall
  Repair iter 1 — re-review (parallel QE+CR)       ~4 min
  Repair iter 2 — coder subprocesses (partial)    ~31 min   ← same pattern
```

**Stage A solved IMPLEMENT.** v3.1's parallel review solved the
review wall. **Repair coder dispatch is now the dominant time sink** —
the per-issue ClaudeCliCoder subprocess loop runs sequentially within
a service, and tier escalation (Haiku → Opus) doubles cost on stuck
issues:

```
[13:28:40] BA-fix1-1 subprocess done in 419.8s   ← Haiku
[13:39:46] BA-fix1-1 subprocess done in 665.7s   ← Opus escalation
                                                    Same issue: 18m total
```

Per-issue average 5-18 min. Repair iter 1 ran 4 issues sequentially
in 38 min when their target files barely overlapped.

The other latent inefficiency that v3.1 inherited from v2:
**Coder and Tester are separate agents with separate LLM contexts**.
They drift — Tester writes against the spec, Coder against the spec
but interprets edge cases differently. The Phase 2a CoderAgentV3 work
already proved a unified agent can produce production-grade code +
tests in one call. The merge logic is right there, never extended to
repair.

## What v4 is

Four architectural changes, applied to **both IMPLEMENT and REPAIR**:

1. **`CoderTesterAgent` — one agent writes code and tests.** Same
   context, no Coder/Tester drift, agent picks its own edge cases
   AND test scenarios. Replaces both v2 Coder and v2 Tester.

2. **Per-issue validation pipeline.** Immediately after the agent
   finishes a single issue, run the deterministic scanners
   (symbol_validator, AST walker, pytest collection check) + a
   brief agentic debug loop. Each issue ships out of this stage as
   either ValidatedIssue (clean) or BrokenIssue (carry forward).

3. **Parallel DAG execution via Kahn's topological sort.** Build a
   dependency graph from (a) `target_files` overlap (conservative
   floor — guaranteed correct) + (b) ServicePlanner-emitted
   `depends_on: List[issue_id]` (additive refinement for logical
   deps that file-overlap misses). Topological levels run
   concurrently, capped at 6 parallel workers by default.

4. **Opus-first for repair, Haiku-default for IMPLEMENT.** No
   Haiku→Opus escalation chain in repair. Repair is by definition
   the harder case (IMPLEMENT already missed there); skip the
   retry. IMPLEMENT keeps the Haiku-default win.

The outer **review feedback loop is unchanged**. v3.1's parallel
QE+CR fan-out + native CoverageReport/CodeReviewReport + V2
approval semantics all carry forward. When review finds new
defects, the repair issues fan out through the same v4 parallel
system.

## Architecture diagram

```
                     ┌────────────────────────────┐
                     │ ServicePlanner             │
                     │   issues + depends_on      │
                     └────────────┬───────────────┘
                                  │
                                  v
              ┌───────────────────────────────────────┐
              │ DAG builder                           │
              │  edges = target_files overlap         │
              │        + planner depends_on (∪)       │
              │  topological sort → levels            │
              └───────────────────┬───────────────────┘
                                  │
                  ┌───────────────┼───────────────┐
                  │ level N (≤6 parallel)         │
                  │                                │
   ┌─────────────┴┐ ┌─────────────┐ ┌─────────────┐
   │ CoderTester  │ │ CoderTester │ │ CoderTester │  ← Opus (repair)
   │   agent      │ │   agent     │ │   agent     │    or Haiku (impl)
   └──────┬───────┘ └──────┬──────┘ └──────┬──────┘
          v                v               v
   ┌──────────────┐ ┌─────────────┐ ┌──────────────┐
   │ Per-issue    │ │ Per-issue   │ │ Per-issue    │
   │ validator    │ │ validator   │ │ validator    │
   │  - symbol    │ │             │ │              │
   │  - AST       │ │             │ │              │
   │  - pytest    │ │             │ │              │
   │  - agentic   │ │             │ │              │
   │    debug     │ │             │ │              │
   └──────┬───────┘ └──────┬──────┘ └──────┬───────┘
          v                v               v
        Validated       Validated      Broken
        Issue           Issue          Issue
                                  │
                                  v
              ┌──────────────────────────────────┐
              │ Next level (waited for level N)  │
              └──────────────┬───────────────────┘
                             │
                            ...
                             │
                             v
              ┌──────────────────────────────────┐
              │ All issues land →                │
              │ v3.1 parallel review (QE+CR)     │
              │ approved? → DONE                 │
              │ else → fan-out repair issues     │
              │   through this same system       │
              └──────────────────────────────────┘
```

## Anchor projections

From tonight's recipe_v4_v31 numbers:

| Phase | v3.1 measured | v4 projected | Why |
|---|---|---|---|
| IMPLEMENT | 18 min | 18 min | unchanged (Stage A) |
| Repair iter 1 — coder | 38 min | **~12-15 min** | 4 issues parallel at level 1 + Opus + no Haiku retry |
| Repair iter 1 — review | 4 min | 4 min | unchanged (v3.1) |
| Repair iter 2 — coder | 31 min (partial) | **~10-12 min** | same |
| **M1 total projection** | ~2h+ (estimated) | **~1h** | ~50% additional cut on top of v3.1 |

Versus the **3h 29m recipe_v3 anchor** (Opus M1 baseline before
v3 work began): **~3.5× wall reduction projected.**

## Concrete decisions

| Decision | Value | Reasoning |
|---|---|---|
| Dep graph source | (a) file-overlap + (b) planner depends_on, union | (b) for logical deps file-overlap misses; (a) as the floor we trust |
| Max parallel coders | **6 default**, configurable in `bizniz.yaml` | Realistic Anthropic Max-plan rate-limit ceiling; tuning knob in config |
| Repair tier list | `["claude-cli:claude-opus-4-7"]` (Opus only) | Tonight's data: tier-0 retry cost 7m on BA-fix1-1 before escalating to Opus anyway |
| IMPLEMENT tier list | unchanged (Haiku default) | BA-fix2-2 perf test still holds: Haiku beats Opus on IMPLEMENT |
| CoderTesterAgent output | structured: `{issue_id, files: [{path, code}], tests: [{path, code}], deps_added, capability_refs}` | One LLM call, one validated envelope |
| Outer review loop | unchanged from v3.1 | parallel QE+CR + V2 approval semantics |
| Stuck issue handling | carry forward as BrokenIssue → next review iter dispatches repair through same system | no nested "fix the fix" loops within a level |

## Implementation surface

```
new modules:
  bizniz/coder_tester/
    agent.py              — CoderTesterAgent (single LLM call,
                            structured output, code+tests)
    prompts.py            — system + user templates
    types.py              — CoderTesterResult, FilledFile,
                            FilledTest, BrokenIssue
    tests/

  bizniz/per_issue_validator/
    validator.py          — runs symbol_validator + AST + pytest
                            collection + brief agentic debug pass
    types.py              — ValidatedIssue, BrokenIssue
    tests/

  bizniz/orchestrator/parallel_issue_runner.py
    PIRunner              — DAG-builder + Kahn topological sort +
                            ThreadPoolExecutor fan-out

modified:
  bizniz/service_planner/scaffolded.py
    + depends_on: List[str] on each emitted issue (planner schema add)

  bizniz/driver/milestone_loop.py
    + use_v4: bool = False flag
    + IMPLEMENT path: when use_v4, dispatch via PIRunner instead of
      CoderAgentV3 single-dispatch
    + REPAIR path: when use_v4, dispatch repair issues via PIRunner
      instead of v3.1's per-issue _code_dispatcher.repair

  bizniz/config/bizniz_config.py
    + max_parallel_coders: int = 6
    + use_v4_repair_tiers: List[str] = ["claude-cli:claude-opus-4-7"]

  examples/v2_build.py
    + --use-v4 flag (implies --use-v3-implement; pairs with --use-v3-1)
```

## Risks + mitigations

1. **Tautological tests.** Same agent grading its own homework — if
   the agent has a misconception, both code and tests encode it.
   **Mitigation**: the per-issue validator's deterministic scanners
   are spec-blind and can't be fooled by the agent's worldview.
   symbol_validator catches hallucinated imports; AST catches
   structural bugs; pytest collection catches missing fixtures /
   syntax errors. The agentic debug pass is a tie-breaker, not a
   gate. Net: deterministic gates are non-negotiable.

2. **Anthropic rate-limit storm.** 6 Opus subprocesses in parallel
   may trigger 429s. **Mitigation**: existing 10/30/60s backoff in
   ClaudeCliClient handles transient 429s; `max_parallel_coders=6`
   is configurable down. Worth a calibration pass — start at 4 if
   first run hits limits.

3. **Dependency graph correctness.** A wrong DAG either over-
   serializes (slow but correct) or under-serializes (race
   conditions, file conflicts). **Mitigation**: file-overlap as the
   conservative floor — guaranteed correct when issues touch
   different files. Planner `depends_on` is additive; never removes
   an edge file-overlap added. Worst case: graph degenerates to
   sequential, behaving like v3.1.

4. **Larger review batches.** When 6 issues land in parallel and
   then review sees them as a unit, inter-issue conflicts (issue A's
   API change breaks issue B's call site) can surface that
   per-iteration review caught earlier. **Mitigation**: per-issue
   validator's pytest collection check fails on import-level
   breakage, catching most cross-issue API mismatches before they
   reach review. Cross-issue *semantic* conflicts (issue A returns
   `{user}` but issue B expects `{user_id}`) still surface in
   review — but that's a fix-issue-pair the review loop creates,
   which the next level handles.

5. **Visibility into stuck issues.** With 4-6 parallel coders, log
   interleaving makes stalls harder to spot. **Mitigation**:
   structured per-issue `[issue_id]` log prefixes (already in v3.1
   coder logs) + a per-issue status tracker that emits a "still
   working after Nm" heartbeat. Operator can scan for issues stuck
   above a threshold.

6. **Convergence drop from same-agent test/code.** The bet is that
   merged context produces *better* tests (no spec-interpretation
   drift). If it's actually *worse*, repair iters increase. **
   Mitigation**: validation plan compares M1 to v3.1's measured
   convergence (77.6% defect drop in iter 1). v4 should match or
   exceed; if it doesn't, revisit the merge.

## Validation plan

Three perf tests before flipping production default:

**Phase 1 — CoderTesterAgent on a single issue**
- Anchor: a fixture from `bizniz/perf_tests/fixtures/` (BA-fix2-2
  or BE-006 — already characterized under v2/v3).
- Pass criteria: produces code + tests in one call; AST clean;
  symbol_validator clean; pytest collects; runtime parity or better
  than v2 Coder + v2 Tester sequential.

**Phase 2 — PIRunner with synthetic 8-issue DAG**
- Anchor: hand-crafted 8 issues with known target_file overlap
  pattern (some independent, some serialized).
- Pass criteria: correct topological ordering; max_parallel=6
  respected; parallel issues actually run concurrently (validated
  via subprocess start-time deltas); total wall ≈ longest level
  chain, not sum of all issues.

**Phase 3 — Full M1 live run on recipe_v4_v4**
- Anchor: tonight's recipe_v4_v31 timings (~2h M1 estimated).
- Pass criteria: M1 wall ≤ 1h 15m; defect convergence ≥ 75% on
  iter 1; no rate-limit hard fails; review approval reached.

If Phase 3 hits: flip `--use-v4` to the canonical recommended path,
mark `--use-v3-1` deprecated.

## Out of scope (deferred to later)

- Cross-service parallel issue dispatch — today's `_code_dispatcher`
  iterates services sequentially. v4 keeps that; intra-service
  parallelism is the leverage point. Cross-service comes later when
  intra-service is settled.
- Adaptive max_parallel — start with the configurable knob; only
  add auto-tuning once we know the realistic ceiling.
- Multi-LLM-backend coder pool (mix Gemini calls in for cheap tier
  during IMPLEMENT) — deferred until Claude-only pipeline is stable.

## Resolved decisions (2026-05-19)

1. **Per-issue agentic debug wall budget: unbounded.** The agent
   loops until clean OR it gives up on its own. Rationale: stuck
   issues are the high-value ones; a hard 60s cap would punt them
   to the next review iter where they'd land in the same parallel
   system anyway, wasting the partial progress. Trade-off: one
   stuck issue extends its level's wall — acceptable because the
   level still benefits from the parallel work on the other 5
   issues.

2. **PIRunner is strictly level-by-level — no speculation.** When
   a level has fewer issues than `max_parallel_coders`, the spare
   workers idle until the level finishes. No speculative pickup
   from level N+1. Keeps the model simple and the dep-graph
   semantics watertight.

3. **`depends_on` is additive-only.** Planner-emitted deps can
   ADD edges to the file-overlap floor; they can never REMOVE one.
   File-overlap is the conservative guarantee; planner refinement
   only ever serializes more, never less. Worst-case: graph
   degenerates to fully sequential, behaves like v3.1.
