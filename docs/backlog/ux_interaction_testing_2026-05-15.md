# Ticket 3 — Interaction Testing Gameplan

Drafted 2026-05-15 as the deliverable for Ticket 3 of
`docs/backlog/ux_followups_2026-05-14.md`. This is a **gameplan
only**; the implementation is broken into follow-up tickets (3a–3d)
at the bottom.

## Problem restatement

Static screenshots catch "page loaded" state. Interaction bugs hide:

- Button loaders stuck, never appear, or jank
- Disabled states that don't visually disable
- Form validation messages flash, never appear, or push layout
- Modal/dropdown open + close transitions
- Hover/focus/pressed states (especially Tailwind utility-order bugs)
- Toasts/snackbars (often visible <1s — easy to miss)
- Table sort/filter/pagination state changes

A multi-agent pipeline that generates apps end-to-end has no
prior knowledge of which interactions exist in any particular project.
The strategy has to work generically across recipe_box, bookshelf,
pet_groomer, and whatever's next.

## Strategies considered

| Strategy | Pros | Cons | Verdict |
|---|---|---|---|
| **A. LLM-emitted scripts per route** | Works without app foreknowledge | Unbounded cost (every page has 20+ interactives); flaky selectors; combinatorial explosion | **Reject for v1** — uncapped LLM dispatch |
| **B. Per-primitive interaction probes** | Bounded cost (1 probe / primitive); reusable across projects; tests what bizniz controls (the design system) | Requires the skeleton's primitives to expose a discovery contract | **Adopt — core of v1** |
| **C. Playwright traces + frame-by-frame** | Catches animations, transitions | Trace analysis is complex; per-step vision cost; new prompt surface | **Defer** — fold into B as needed |
| **D. DOM snapshot diffing** | Cheap, fast, deterministic — no vision cost per step | Misses purely visual bugs (CSS, alignment, color) | **Adopt — pair with B** as cheap signal |
| **E. Storybook play tests** | Industry-standard, strong contracts | Skeleton rewrite (~weeks) — every skeleton needs Storybook | **Defer** — revisit if B+D plateau |

## Chosen approach: B + D combined

### Why B+D wins for v1

- **B (per-primitive probes)** is bounded cost AND tests the part of
  the app bizniz directly controls (the design-system primitives).
  Per-route LLM dispatch (A) is unbounded; per-primitive is finite.
- **D (DOM snapshot diffing)** is cheap enough to use as the fast
  signal inside each probe. Vision evaluation runs only on frames
  that DOM diff flags as interesting.
- Together they catch ~80% of interaction bugs at ~30% the cost of
  pure LLM-driven scripts.

### Architecture

```
ProUXDesigner.review_frontend()
  ├─ ... existing phases (code_review, global_design, per_view) ...
  └─ NEW: interaction_phase()
       │
       ├─ discover_primitives(workspace)
       │     ↓
       │     scan src/components/ui/* → identify primitives by name
       │     + (later) parse data-bizniz-primitive attribute for
       │     primitives that don't match conventional filenames
       │
       ├─ probe_primitive(name, workspace, compose_path)  ← per primitive
       │     │
       │     ├─ select probe script by primitive type:
       │     │   - Button: render, click, capture loading state,
       │     │     capture disabled state, capture hover/focus
       │     │   - Modal: open trigger, capture open, click backdrop,
       │     │     capture closed
       │     │   - FormInput: empty → invalid input → valid input →
       │     │     submission states
       │     │   - Toast: trigger, capture appearing, wait, capture
       │     │     gone
       │     │   - DataTable: capture default, sort column, capture
       │     │     sorted, filter, capture filtered
       │     │
       │     ├─ run script in Playwright sidecar
       │     │   - Each step: screenshot + DOM snapshot
       │     │
       │     ├─ DOM diff between steps
       │     │   - Did the expected DOM change happen?
       │     │     (loading attribute set; dialog role appeared;
       │     │     error message visible; sorted-asc class on header)
       │     │   - DOM-diff PASS → primitive is structurally healthy
       │     │   - DOM-diff FAIL → flag the frame for vision eval
       │     │
       │     └─ vision eval ONLY on flagged frames
       │         - "This is the loading state of a Button after click.
       │           Does it look correct (loader visible, button
       │           disabled, no layout shift)?"
       │
       └─ aggregate primitive_health into result["primitive_health"]
            with shape:
            { "Button": {"status": "passed",
                         "frames_captured": ["click", "loading", "disabled"],
                         "issues": []},
              "Modal":  {"status": "failed",
                         "frames_captured": ["open"],
                         "issues": [{"frame": "open",
                                     "description": "..."}]},
              ... }
```

### Cost model

- Per-primitive probe: ~30s Playwright + ~10s DOM diff = ~40s
  baseline. Vision eval adds ~30s when triggered (~3 of 5 primitives
  typically).
- Per project: 5-7 primitives = **3-7 minutes** added per run.
- Comparable to one extra route review. Cacheable per-primitive
  (re-fire only when the primitive source file changes).

### What the skeleton has to provide

For B to work, the skeleton's primitives need a small discovery
contract:

1. **Conventional filenames** — already in place. We grep
   `src/components/ui/*.tsx`.
2. **Data attribute** — each primitive's root element exposes
   `data-bizniz-primitive="button"` (or `modal`, etc.). Cheap to
   add to the skeleton, makes DOM diff selectors trustworthy.
3. **State attributes** — primitives that have states expose them
   on the root element: `data-state="loading"` on a Button while
   submitting, `data-state="open"` on a Modal. Tailwind's `data-*:`
   variants already incentivize this pattern.

The state-attribute contract is the biggest skeleton lift. The
payoff is huge: DOM-diff becomes a one-line check (`element.matches(
'[data-bizniz-primitive="button"][data-state="loading"]')`).

## Caching

Each primitive probe is cached on the source file mtime of the
primitive component:

- `src/components/ui/Button.tsx` mtime newer than cached run →
  re-fire Button probe.
- Otherwise → reuse last result.

Cache shape mirrors `review_store` — keyed by
`(project_slug, primitive_name)`, stored at
`<workspace>/.bizniz/primitive_reviews.db`.

## Follow-up tickets

The actual implementation breaks into four focused tickets:

### Ticket 3a — Skeleton primitive contract

- Add `data-bizniz-primitive` + `data-state` to the React skeleton's
  Button, Modal, FormInput, Toast, DataTable.
- Document the contract in SKELETON.md.
- Add a skeleton test that asserts each primitive renders the
  expected data attributes.
- **Acceptance:** Building a fresh project from the skeleton shows
  the data attributes on rendered HTML.

### Ticket 3b — Primitive discovery module

- `bizniz/ux_designer/primitive_discovery.py`
- Tier 1: parse filenames in `src/components/ui/`.
- Tier 2: grep for `data-bizniz-primitive` in workspace.
- Returns `List[PrimitiveSpec]` (name, source_file, type).
- **Acceptance:** Unit tests cover both tiers + missing-primitive
  fallback.

### Ticket 3c — Interaction probe runner

- `bizniz/ux_designer/interaction_probe.py`
- One probe function per primitive type (Button, Modal, FormInput,
  Toast, DataTable to start).
- Each probe emits a Playwright script + DOM diff checkpoints.
- Runs in the existing sidecar; returns `PrimitiveProbeResult`.
- Cache by primitive source-file mtime (mirror review_store).
- **Acceptance:** Probe runs locally against recipe_box, catches a
  manually-introduced bug (e.g. break Button's loading state).

### Ticket 3d — Wire into ProUXDesigner

- Add `interaction_phase` after the per-view loop in
  `review_frontend`.
- Aggregate primitive_health into the run result.
- Surface in run summary log + run_log JSONL.
- Disable via flag `--no-interaction-tests` initially so we can
  ramp it up.
- **Acceptance:** End-to-end run on recipe_box reports per-primitive
  health; primitive cache means second run skips clean primitives.

## Open questions (flag for later)

1. **Custom primitives.** Some apps grow their own primitives outside
   `components/ui/`. Tier 2 (data-attribute grep) handles most; what
   about projects that don't follow even that convention? → Defer:
   the skeleton enforces the contract; off-skeleton primitives are
   out of scope for v1.
2. **CRUD-flow coverage.** Does the probe need to test the full
   "user fills form → submits → row appears in table" flow, or is
   per-primitive enough? → v1 says per-primitive. CRUD-flow is an
   integration-test concern; the existing HTTPApiTester +
   WebUITester cover it.
3. **Cache invalidation for global style changes.** When global
   design rewrites Button.tsx, all primitive probes should refire.
   → Mirror plan_cache's `files_written_mtimes` pattern.
4. **Hover / focus / pressed states.** Static screenshots can't
   capture these. Playwright supports forcing `:hover` via
   `force: true`. Add to Button probe in 3c.

## Why this order

- **3a first** because nothing else works without the data-attribute
  contract.
- **3b** is pure analysis; can ship in parallel with 3c.
- **3c** is the meat. Probably the largest of the four.
- **3d** is integration glue — fast once 3a/b/c land.

Estimated cycle time: 3a half-day, 3b half-day, 3c two days, 3d
half-day. Total ~3.5 dev days for a working interaction-test phase.
