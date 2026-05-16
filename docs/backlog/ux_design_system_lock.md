# UX Design System Lock — Establish Once at M1, Skip in M2+

Filed 2026-05-15. Sub-ticket of roadmap item 2 (Finish UX with
Storybook).

## Problem

`ProUXDesigner._apply_global_design` runs on every milestone whose
`plan_cache` misses. The plan_cache fix from earlier this session
prevents global_design from self-invalidating (excludes its own
outputs from the mtime fingerprint), but **does not** prevent
invalidation from legitimate IMPLEMENT-phase writes (new pages, new
components, new routes).

Result: every milestone re-derives the palette + typography +
primitive component signatures. The model keeps the system *similar*
between runs (terracotta stays terracotta) but **subtly drifts** —
hex values shift by a digit, primitive prop signatures rearrange,
generated CSS classes get marginally different selectors. Net cost
per build:

- ~12 min × 4 non-M1 milestones = **~48 min of Claude wall clock**
- 20-25 files rewritten per milestone × 4 = **80-100 churn-rewrites**
  that downstream code has to accommodate
- Visual jitter the user sees between milestones (palette shift, font
  weight tweak)

This is wrong by design intent: a design system is supposed to be
*established* and then *applied*, not re-derived every time the
spec grows.

## Right architecture

### M1 — establish

`ProUXDesigner.establish_design_system(milestone, workspace, ...)`:
- Runs code_review → emits design plan
- Runs apply_global_design → writes tokens + primitives
- Writes a **lock file** at `<workspace>/.bizniz/design_lock.json`:
  ```json
  {
    "established_at": "2026-05-15T...",
    "milestone_index": 0,
    "design_plan": { ... },
    "primitives": ["Button", "Modal", "FormInput", "DataTable", ...],
    "tokens": {
      "palette": { ... },
      "typography": { ... },
      "radii": { ... }
    },
    "files_managed": ["tailwind.config.ts", "src/index.css", "src/components/ui/Button.tsx", ...]
  }
  ```

### M2-M5 — apply or extend

`ProUXDesigner.review_frontend(...)`:
1. Load `design_lock.json`. If missing → fall through to
   `establish_design_system` (first-milestone path).
2. If lock exists, **skip code_review + apply_global_design
   entirely**.
3. If new primitives are needed (e.g. M3 needs `KanbanColumn` that
   M1 didn't ship): run a light `extend_primitives(primitive_names,
   design_lock)` step. Takes the established tokens as INPUT, only
   ADDS new primitive files. Never touches `tailwind.config.ts`,
   `index.css`, or existing primitive files. Updates the lock with
   the new primitive names.
4. Per-view loop runs normally — that's still per-milestone work.

### Trigger for full re-establish

Two ways the full `establish_design_system` re-runs:
- User passes `--redesign` flag to `v2_build` (explicit ask).
- Problem statement mentions "redesign" / "restyle" /
  "rebrand" — Planner emits a milestone with
  `redesign_after=True`, similar to the existing `refactor_after`
  flag.

Both routes invalidate the lock and start fresh.

## Acceptance

1. Back-to-back milestones on the same project produce **zero
   writes** to:
   - `tailwind.config.{ts,js,cjs}`
   - `src/index.css` / `src/styles/index.css`
   - `src/components/ui/*` files that existed at the end of M1
2. The lock file faithfully captures what was established at M1.
   Removing the lock + re-running M2 produces identical results to
   running M2 normally.
3. New primitives needed by later milestones (e.g. Modal in M3,
   KanbanColumn in M4) get added without touching established
   tokens. Verifiable: `tailwind.config.ts` mtime never updates
   after M1.
4. `--redesign` flag (new) re-establishes from scratch.
5. The CRM project rebuilt with this change: total UX time drops
   from ~5 × 12 min = 60 min to ~12 min (M1) + 4 × 1 min (M2-5
   lock checks + maybe extend) = ~16 min. **~44 min saved per
   build.**

## Why this fits roadmap item 2

Item 2 ("Finish UX with Storybook") rewrites the UX phase so it
iterates the Storybook catalog rather than route screenshots. That
rewrite is the right place to also separate "design system
establishment" from "per-view review" — they're different
operations that today are conflated inside `review_frontend`.

Order: do this lock work as part of item 2's design phase, before
the Storybook iteration is wired up. The Storybook iteration then
consumes the locked design system (it doesn't need to re-derive
tokens to render stories).

## Cost-of-postponing

If item 2 stays open for a while, this is ~48 min per build of
wasted time. The user's CRM build today exercises this 4 times
(M2/M3/M4/M5). Multiply by every test project we build during
roadmap item 8 (Claude perf test) and item 9 (Gemini baseline) —
the waste compounds.

If we hit a bunch of test builds before item 2 ships, consider
landing a minimal version of this lock independently as a sub-sub-
ticket.

## Related

- Earlier session's UX Ticket 1 (`docs/backlog/ux_followups_2026-05-14.md`)
  — fixed *one* class of plan_cache invalidation (self-writes).
  This ticket fixes the *other* class (legitimate IMPLEMENT writes).
- `bizniz/ux_designer/plan_cache.py` — the cache infrastructure
  this work extends.
- `bizniz/ux_designer/pro_ux_designer.py` — `_apply_global_design`
  is the method to split / gate.
