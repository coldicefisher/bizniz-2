# UX — `_take_view_screenshots` orphan-shot fallback substitutes wrong screenshots

Filed 2026-05-15 mid-CRM-build M4. Sub-ticket of roadmap item 2.

## Problem

`ProUXDesigner._take_view_screenshots` has a fallback that fires
when `_bucket_shots_by_route` returns zero matches for the route
we asked about:

```python
# Fallback: model didn't emit a test with the right
# requested_route metadata. Take the shots with no
# meta (older capture path) or accept all — better
# than nothing.
shots = [
    s for s in all_shots
    if not Path(s["path"]).with_suffix(".meta.json").exists()
] or all_shots
```

"Better than nothing" turns out to be **worse than nothing** in
practice. Caught in M4 of crm_v1:

- The generated Playwright script never produced a `dashboard.png`
  (only `deals-list-searched.png`, `deal-stage-dropdown.png`, etc.).
- `_bucket_shots_by_route` correctly returned 0 matches for
  `/dashboard`.
- Fallback grabbed orphan PNGs (the two deals files that had no
  meta sibling) and used them as the "dashboard" capture.
- Vision model evaluated *deals screenshots* against the
  *dashboard design spec*. Score 3/10 first iter, 1/10 second.
- Coder dispatched fixes for "dashboard issues" based on what was
  actually a deals view. Files got modified for no reason.

The Ticket 2 short-circuit (`not_reviewable` on capture mismatch)
specifically checks `meta.final_pathname` against the requested
route — it doesn't fire when there's NO meta, because we'd
filter-out-then-fallback to non-meta shots.

## Fix

`_take_view_screenshots` should treat "zero bucket matches AND no
matching capture in workspace" as a hard-fail for this route
(returns empty), letting `_view_iteration` flag the view
`not_reviewable` — same as the existing capture-mismatch path.

The fallback to non-meta shots should NEVER substitute a shot
from a DIFFERENT route. Two acceptable behaviors:

  1. Return the non-meta shots ONLY IF the route in question was
     the only route in the capture request. Otherwise return [].
  2. Drop the fallback entirely. If `_bucket_shots_by_route`
     returned 0 for this route, the Playwright script didn't
     capture it; the right move is to fail this view cleanly
     (`not_reviewable`), not substitute a random unrelated shot.

## Acceptance

- A view whose route was not captured by the Playwright script
  gets marked `not_reviewable` with reason
  `"no screenshot captured"`. App score excludes it. Coder is NOT
  dispatched with fixes based on an unrelated screenshot.
- Regression test: a fake workspace with PNGs for routes A and B
  (no metas) and a `_view_iteration` call for route C must
  produce `not_reviewable=True`, not substitute A or B's PNG.

## Why this fits roadmap item 2

Item 2 reworks the UX phase to iterate Storybook stories instead
of route screenshots. Stories have deterministic file
naming + per-story DOM diffs, so the orphan-shot substitution
problem disappears entirely. Land this fix as part of item 2's
"replace the route-screenshot pipeline" work.

If item 2 stays open for a while and we keep building test
projects, this is misleading every multi-route review run.
Consider landing the focused 5-line fix independently.

## Related

- `bizniz/ux_designer/pro_ux_designer.py:_take_view_screenshots`
  — the function with the bug.
- `docs/backlog/ux_followups_2026-05-14.md` Ticket 2 — handled the
  meta-says-wrong-page case; this is the no-meta-at-all case.
- `docs/backlog/ux_design_system_lock.md` — also under item 2,
  filed today.
