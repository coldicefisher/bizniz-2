# UX Designer Followups — opened 2026-05-14

Surfaced after the v2.11 validation run (recipe_box, APP SCORE 7.8/10
with route resolver + prompt fix landed). Three tickets to work next.

---

## Ticket 1 — Stabilize global design across runs

**Problem.** The "global design" step (`_apply_global_design`)
rewrites `tailwind.config.ts`, `src/index.css`, primitive components
(`src/components/ui/*`), and adopts them in existing pages on every
plan-cache miss. Recipe_box v2.11 reported `global_design=227.8s`
even though no user prompt changed the visual direction since v2.10.

The site's styling subtly drifts run-to-run because the LLM regenerates
the same intent into slightly different code each time.

**Root cause.** `plan_cache.compute_input_mtime()` watches every
`src/**/*.{ts,tsx,jsx,js,css,scss}` file. The global design step
**writes** into that watched set on every run (40+ files in v2.10).
So the cache is structurally guaranteed to invalidate on the next
run — it can't help.

**Fix direction (pick one or combine):**

1. **Idempotent global design.** Hash the design plan + the current
   set of design-token files. If the plan didn't change AND the
   tokens already reflect it, skip the dispatch entirely. Coder
   diffs the writes against current content; no-op writes don't bump
   mtimes.
2. **Intent-keyed cache, not file-mtime cache.** Save the plan +
   global_fix_result keyed by the design intent fingerprint
   (`app_type`, palette, typography, primitives list). Re-fire only
   when intent diverges or the user explicitly evolves.
3. **Gate on a user signal.** "Evolve mode" or a problem-statement
   change → re-run global. Otherwise skip and only re-run *per-view*
   loops. Build-mode default: run global once at M1, never again.

**Acceptance.** Back-to-back runs on the same project with no input
changes produce **zero** writes from the global design step. Reported
`global_design` timing reads ≤1s on the second run (just the
idempotency check).

**Why this matters.** Beyond cost (227s per run, $X cumulative), the
user's lived experience is the SAME site shifting in subtle ways
each time they iterate — the opposite of design discipline.
See `[[user_jamey_goal]]` — shipped SaaS sites is the goal; visual
churn between runs is anti-shippable.

---

## Ticket 1b — Tailwind chain-fix false-positives on dev-server warm-up

**Surfaced 2026-05-15 in the v2.12 cache-hit validation.** With both
plan_cache + route resolver hitting (run dropped from 832s → 164s),
`build_chain_fix` unexpectedly fired for 120s — even though the
prior run (v2.11) reported `tailwind_wired=True` and the site
visually rendered correctly.

**What happened.**

- `_verify_tailwind_serving` fetched the rendered HTML + linked
  stylesheets and grepped for `--tw-*` markers.
- The dev container had restarted between v2.11 and v2.12 (compose
  was down or recycled). Vite hadn't finished its initial PostCSS
  build yet when the verifier probed.
- CSS body came back without Tailwind markers → verifier returned
  `ok=False` → `_fix_build_chain` dispatched a heavyweight Coder
  repair pass.
- Coder's "fix" took 120s, found nothing actually wrong, and the
  re-check passed (Vite had finished warming up by then).

So the verifier is racing Vite's warm-up. The repair is a no-op
that costs 120s every time the container restarts.

**Fix directions (pick one or combine):**

1. **Retry probe with backoff.** Hit the CSS endpoint 3× over
   ~30s before declaring failure. Vite warm-up is usually <20s.
   Cheap, robust to most races.
2. **Wait for Vite ready signal.** Vite exposes `__vite_ping` and
   logs `ready in <N> ms`. Probe one of those first, then probe CSS.
3. **Distinguish empty-CSS from missing-Tailwind.** If the served
   CSS body is <100 bytes or contains build-pending markers, treat
   as warm-up not failure. If it has substantive content lacking
   `--tw-*`, that's a real Tailwind miss.
4. **Skip build_chain_fix when prior run's cache says it was
   wired.** If `cached_global_fix_result.tailwind_wired=True`, give
   the verifier a generous retry budget before triggering repair —
   we know the config is right.

**Acceptance.** Back-to-back runs on a project with no real changes
should NOT fire `build_chain_fix`. The verifier waits long enough
for Vite to settle, then either confirms Tailwind is wired or
correctly flags a real config problem.

**Related.** Same shape as Ticket 1 — heavy work re-firing because
a transient state was misread as an input change.

---

## Ticket 2 — Capture mismatches: enumerate + handle

**Problem.** `_verify_capture` flags routes where the captured
pathname doesn't match the requested route. Confirmed cases from
v2.11:

- `/admin` → captured `/admin/users` (admin index → first sub-route redirect)
- `/recipes` → captured `/dashboard` (probably an auth-redirect or default-view)

The verifier correctly skips the Coder fix dispatch when this fires,
but the route is still scored as if the wrong page were the real
page. That pollutes the app score and the review_store cache.

**Investigation (do this first).**

1. Grep the last 3 runs' logs for "capture mismatch" + sift the
   `requested_route` vs `final_pathname` pairs. Bucket by cause:
   - **Index redirect** — `/admin` resolves to `/admin/users` because
     the SPA has a `<Route index>` that redirects.
   - **Auth redirect** — protected route → `/login` because
     storageState injection failed (we usually catch this elsewhere
     but check for residuals).
   - **Default-view redirect** — `/recipes` → `/dashboard` because
     of a stale link, role gate, or feature flag.
   - **Sibling collision** — already handled in `_verify_capture`,
     but verify it's still firing correctly on dynamic routes.

2. For each bucket, pick the right handling:
   - (a) Mark `not_reviewable` in the result, don't score it, don't
     persist to review_store. The app-score aggregate ignores it
     instead of factoring in a misleading 4-5/10.
   - (b) Update the route discovery to **follow** the redirect and
     review the destination as the de facto route (then dedupe).
   - (c) Flag back to the engineer phase: "the SPA is redirecting
     `/recipes` → `/dashboard`; is that intentional?" — surfaces
     real product bugs.

**Acceptance.** Each mismatch class has documented handling. The
review_store no longer caches a "passing" entry for a route that
was actually a redirect. The APP SCORE excludes `not_reviewable`
routes from `covered` count.

---

## Ticket 3 — Interaction testing strategy (clicks, loaders, button states)

**Problem.** Static screenshots only catch the "page loaded" state.
Common UX bugs hide in interactions and are invisible to the current
pipeline:

- Button loaders that get stuck, never appear, or appear/disappear
  jankily
- Disabled states that don't visually disable (still look clickable)
- Form validation messages that flash, never appear, or block the
  layout
- Modal/dropdown open + close transitions
- Hover / focus / pressed states (especially with Tailwind utility
  ordering bugs)
- Toasts and snackbars (appear and disappear in <1s — easy to miss)

**Gameplan to brainstorm AFTER Tickets 1 + 2 ship.** Options to
weigh:

- **LLM-emitted interaction scripts.** Per route, ask the model to
  enumerate interactive elements + emit a short "do action X, then
  screenshot" script. Risk: combinatorial explosion + LLM cost.
- **Per-primitive interaction probes.** A canonical Button.test,
  Modal.test, Form.test that every project's primitive must pass.
  Lives in the skeleton, runs in the UX phase.
- **Playwright traces + frame-by-frame.** For animations / loaders,
  record a trace and sample frames at known checkpoints. More
  expensive but catches transition bugs.
- **DOM snapshot diffing.** Capture innerHTML at each interaction
  step; diff to detect missing/wrong elements without vision cost.
- **Storybook integration.** If the skeleton ships Storybook, capture
  each story's `play` interaction sequence. Strong signal at low cost.

**Acceptance (of the gameplan, not the implementation).**
Documented decision on which strategy/combination to ship first,
broken into ticket(s) under this one.

**STATUS 2026-05-15:** Gameplan delivered at
`docs/backlog/ux_interaction_testing_2026-05-15.md`. Chose **B + D**
(per-primitive probes + DOM snapshot diff). Broken into four
follow-up tickets:
  - 3a — Skeleton primitive contract (`data-bizniz-primitive`,
    `data-state`)
  - 3b — Primitive discovery module
  - 3c — Interaction probe runner (one probe per primitive type)
  - 3d — Wire into ProUXDesigner as `interaction_phase`

Estimated cycle time: ~3.5 dev days for a working v1.

---

## How these relate

- **Ticket 1** is the cheapest and the most user-visible (every run
  costs ~$X and 4 minutes for nothing). Do first.
- **Ticket 2** is medium effort and improves the signal-to-noise of
  APP SCORE. Do second.
- **Ticket 3** is the biggest scope. Strategy gameplan only — actual
  implementation is its own milestone.

Each ticket cuts independently — feel free to ship in any order, but
Ticket 1's win is large enough to do it now.
