---
name: Pet-groomer buildout plan — bizniz first real customer
description: Iterative milestone-driven buildout of pet-groomer to validate the full bizniz pipeline (build → evolve → review → refactor → regress)
type: project
originSessionId: 44c643bd-6fd0-4168-b18b-8f23a5343205
---
Plan set 2026-05-01. Pet-groomer is the first real customer of the bizniz pipeline — small enough to iterate fast, rich enough to exercise every mode.

**The pipeline being validated:**

1. **Harder, more in-depth prompt up front** — replace the current "Build a pet grooming web app" with a prompt rich enough that the planner has to think. Probably includes specific user flows, business rules (no double-booking), realtime updates, payment integration, etc.

2. **Planner decomposes into milestones** — `planner_model` (gemini-pro per the model config) takes the rich prompt and emits N milestones, each with a slice of the problem. Existing `architect.evolve()` accepts a milestone with `problem_slice`.

3. **Iterative milestone execution** — for each milestone:
   - Architect.build() (M1) or architect.evolve() (M2+)
   - Per-run snapshot saved (architecture digest, integration tests, SKELETON.md, contracts) — the four v0 artifacts that bridge build/evolve modes
   - Integration tests run against the live stack at the end of each milestone
   - Snapshot serves as anchor for the NEXT milestone's evolve agent

4. **Shape evaluation** — once enough milestones produce a recognizable v1 shape (probably 3-5), pause iterating.

5. **Final tests** — full integration suite + smoke test of the live stack.

6. **Manual review** — human (the user) reviews:
   - Generated code
   - Generated docs
   - Generated tests
   - Architecture decisions

7. **Reduce / refactor** — agent or human-driven cleanup pass. Remove dead code, consolidate duplicated patterns, simplify over-engineering.

8. **Regression tests** — re-run the integration suite after the reduce/refactor pass to confirm no behavior changed.

9. **Evaluate** — measure: cost per milestone, time per milestone, escalation rate (gemini-flash → pro), human-edit cost in the review gate, regression count post-refactor.

**Why this works:**
- Pet-groomer is small (<10 services likely), so each milestone fits comfortably in build/evolve scope
- The "manual review + reduce" gate before regression tests is the part most pipelines skip — having a human gate makes the integration tests trustworthy as the durable contract going forward
- Milestone snapshots build up the v0-artifact ecosystem we need for evolve mode to work later

**What this plan exercises across bizniz:**
- ✅ Build mode (already shipped)
- ⬜ Evolve mode (`architect.evolve()` stub exists but not connected to planner output yet)
- ⬜ Planner-driven milestone decomposition (planner_model exists but milestone flow not wired end-to-end yet)
- ✅ Integration phase (HTTPApiTester) — already shipped
- ✅ Run reports + snapshots (per-run JSON sidecars + markdown) — already shipped
- ⬜ Reduce/refactor agent — not built yet, may be human-driven first iteration

**Why:** The discoverer→business_manager→marketer→builder pipeline produces apps customers iterate on. We need to prove the full cycle (build, then add features, then reduce, then regress) works on a real example before claiming bizniz can ship customer-facing apps. Pet-groomer is the rehearsal.

**How to apply:**
- When the user says "let's start building pet-groomer" — the first move is to draft the harder prompt, not to immediately call `architect.build()`. Give the planner real material to decompose.
- Save snapshots aggressively — at end of each milestone run, capture the four v0 artifacts. They're free to produce; expensive if missing later.
- The manual review gate is on the user, not bizniz. Don't try to automate it.
- Track cost per milestone explicitly — bizniz cost-tracker already does this; surface per-milestone deltas in run reports.
- Don't skip the regression step. The reduce/refactor pass will break things; the integration suite catches it.
- Consider the planner output (milestones) as v0-artifact-zero — save it alongside the architecture digest.

**Status as of 2026-05-01:** plan captured; pipeline is V6 of pet_groomer running with full integration phase enabled. Once V6 lands cleanly, that's milestone 1 baseline. Subsequent milestones can begin.
