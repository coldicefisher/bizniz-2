# 2026-05-01 — Pet-groomer buildout plan

The first real customer for the bizniz pipeline. Used to validate
the full build → evolve → review → refactor → regress cycle before
claiming bizniz can ship customer-facing apps via the
discoverer→business_manager→marketer→builder pipeline.

## The plan

1. **Draft a harder, more in-depth prompt up front.** Replace the
   current "Build a pet grooming web app" with a prompt rich enough
   that the planner has to think — specific user flows, business
   rules (no double-booking), realtime updates, payment
   integration, etc. The prompt is the input; quality matters.

2. **Planner decomposes into milestones.** `planner_model`
   (`gemini-pro` per current config) takes the rich prompt and
   emits N milestones, each with a problem slice. The existing
   `architect.evolve()` accepts a milestone with `problem_slice`,
   so the wiring is mostly there.

3. **Iterative milestone execution.** For each milestone:
   - M1: `architect.build()` (greenfield)
   - M2+: `architect.evolve()` (extends existing)
   - Per-run snapshot saved (the four v0 artifacts: architecture
     digest, integration tests, SKELETON.md, contracts)
   - Integration tests run at end of milestone
   - Snapshot serves as anchor for the NEXT milestone's evolve agent

4. **Shape evaluation.** After 3-5 milestones produce a recognizable
   v1 shape, pause iterating.

5. **Final tests.** Full integration suite + smoke test of the live
   stack.

6. **Manual review (human gate).** The user reviews:
   - Generated code
   - Generated docs
   - Generated tests
   - Architecture decisions

   This step is non-negotiable. The integration tests become the
   durable contract for evolve mode going forward, so they need to
   represent behavior a human signs off on.

7. **Reduce / refactor.** Cleanup pass — could be agent-driven
   later, but probably human-driven for the first iteration. Remove
   dead code, consolidate duplicated patterns, simplify
   over-engineering.

8. **Regression tests.** Re-run the integration suite after the
   reduce/refactor pass. The reduce step will probably break
   something; this catches it.

9. **Evaluate.** Measure:
   - Cost per milestone
   - Time per milestone
   - Escalation rate (gemini-flash → pro)
   - Human-edit cost in the review gate
   - Regression count post-refactor

## What this exercises across bizniz

| Capability | Status |
|---|---|
| Build mode (`architect.build()`) | ✅ shipped |
| Integration phase (HTTPApiTester) | ✅ shipped 2026-05-01 |
| Run reports + per-run snapshots | ✅ shipped |
| Skeleton-aware engineer prompts | ✅ shipped 2026-05-01 |
| Universal SKELETON.md contract | ✅ shipped 2026-05-01 |
| Evolve mode (`architect.evolve()` stub) | ⬜ exists but not connected to planner output |
| Planner-driven milestone decomposition | ⬜ planner_model exists but flow not wired end-to-end |
| Reduce/refactor agent | ⬜ not built yet — manual first iteration |

## Why pet-groomer is the right rehearsal

- Small enough to iterate fast (<10 services)
- Rich enough to exercise every mode if we extend it (auth + CRUD +
  realtime + payments would cover most patterns)
- Already validated through V5/V6 builds — the baseline works
- Familiar domain — easy for the user to spot when generated code
  is wrong

## What we're not doing yet

- Not building the discoverer/business_manager/marketer agents.
  Pet-groomer is hand-driven — the user provides the prompt. The
  upstream agents come AFTER pet-groomer proves the pipeline works.
- Not building the reduce/refactor agent. Manual first time around.
- Not auto-approving milestone gates. Human in the loop until trust
  is earned.

## Sequencing

1. Land V6 cleanly (current run — full integration phase
   end-to-end). That's milestone 1's baseline.
2. Draft the harder prompt for pet-groomer M1.
3. Wire planner to emit milestones from the harder prompt.
4. Run M1 → review → reduce → regress → snapshot.
5. Run M2 (evolve mode) → review → reduce → regress → snapshot.
6. Continue until shape is solid.
7. Final review + reduce + regression suite.
8. Document the cost/time numbers as the bizniz baseline efficiency
   profile.

## Related work

- `bizniz/integration/` — produces v0 artifact #2 (integration tests)
- `bizniz/workspace/skeleton_conventions.py` — produces v0 artifact #3
- `bizniz/integration/contracts.py` — produces v0 artifact #4
- `bizniz/run_report/` — produces v0 artifact #1
- `bizniz/architect/architect.py` `evolve()` — entry point for M2+
- `bizniz/planner/` (presumed) — needs the harder-prompt-to-milestones flow
- See `docs/changes/2026-05-01_build_vs_evolve_strategy.md` for the
  strategic framing this plan executes against.
