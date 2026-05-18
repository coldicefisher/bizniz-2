# coder_be006_ab — Decomposer cost-benefit on BE-006

**Question:** does Decomposer make Coder faster, slower, or the same
on a real CRUD-router issue? And does it cost or gain quality?

**Tests:**
- `bizniz/perf_tests/tests/coder_be006_fat.py` — single Coder dispatch
  for the entire BE-006 scope (POST + GET list + GET one + PUT + DELETE +
  UUID coercion).
- `bizniz/perf_tests/tests/coder_be006_decomposed.py` — 7 serial Coder
  dispatches for the same scope, one per `BE-006-U1..U7` unit.

Both run against the same `coder_be006_decomposed/workspace_seed/`
fixture (a recipe_v2-class skeleton + Recipe model + schemas + helpers).

## Results — first run pair

| | fat | decomposed | delta |
|---|---|---|---|
| **wall clock** | **296.4s** (4m 56s) | **1196.3s** (19m 56s) | **+899.9s (+304%)** |
| coder calls | 1 | 7 | +6 |
| median call | 296s | 179s | -39% per call |
| structural match | 12/12 | 12/12 | same |
| AST parse | ok | ok | same |
| symbol validation | passed (12 resolved) | passed (12 resolved) | same |
| file size | 6640 bytes | 8249 bytes | +24% |
| top-level defs | 9 | 8 | -1 |
| git rev | `b955e729` | `b955e729` | same |
| tags | `perf/coder_be006_fat/run-1` | `perf/coder_be006_decomposed/run-1` | |

### Per-unit timings (decomposed)

| unit | title | elapsed |
|---|---|---|
| U1 | Create router module + imports + deps | 122s |
| U2 | POST /api/v1/recipes (create) | 171s |
| U3 | GET /api/v1/recipes/mine (list) | 169s |
| U4 | GET /api/v1/recipes/{id} (read) | 179s |
| U5 | PUT /api/v1/recipes/{id} (update) | 191s |
| U6 | DELETE /api/v1/recipes/{id} | 180s |
| U7 | UUID 422→400 coercion | 183s |

Per-unit p50: 179s. Min 122s (U1, scaffold only). Max 191s (U5).
Tight distribution — no unit is "the cheap one"; each carries a
near-flat ~3-min overhead.

## What this answers

### 1. Decomposer is **net-negative on wall clock** here.

The fat dispatch handled the same scope **4x faster** end-to-end.
The decomposer's per-unit "smaller surface" advantage doesn't
recover what's lost to 7x the per-call setup cost (LLM cold-start,
discovery tool loop on already-edited files, context re-build).

If this ratio holds at scale, recipe_v2 M2 (24 production issues
decomposed into ~80 units) would pay roughly:

```
24 issues × 4 min (fat baseline) = 96 min
80 units  × 3 min (per-unit obs) = 240 min
                                  ────────
                       penalty ≈ +144 min on M2 alone
```

This is consistent with the M2 walkthrough showing Coder dispatch
as 92% of IMPLEMENT phase — and IMPLEMENT as 61% of M2 wall time.

### 2. Quality is **at parity** on this scope.

- Both outputs parse cleanly via `ast.parse`.
- Both pass `bizniz.coder.symbol_validator` with zero unresolved
  symbols, zero hallucinated attributes, zero syntax errors.
- Both match all 12 structural patterns (router shape, all HTTP
  decorators, auth dependency, correct status codes, 404 + 400).
- Fat's output is tighter (190 lines / 9 defs). Decomposed's is
  more verbose (219 lines / 8 defs) — the extra bytes come from
  per-unit boilerplate that didn't get unified across calls.

The decomposed output is NOT lower-quality. It's just longer for
no functional gain.

### 3. What we still **don't** know

- Whether decomposed wins on issues that the fat call **fails on**.
  This run had a fat dispatch succeed first-try; the hypothesis that
  decomposed reduces escalation rate isn't tested here.
- Whether decomposed has lower variance across runs. One sample each.
- Whether issue families exist where decomposed beats fat (e.g.,
  highly-coupled refactors vs greenfield CRUD).

## Implications for the broader perf question

This is the first hard data point that the **Decomposer is the
direct cost driver** for the 30-50x regression we saw in
recipe_v2. The math now fits:

```
recipe_v2 M2 observed:       3h 10m IMPLEMENT
fat-projection M2:           ≈ 1h 36m  (24 issues × 4 min)
decomposed-projection M2:    ≈ 4h 00m  (80 units × 3 min)
```

Decomposed projection lands within the observed range. The
fat-projection is in the bookshelf_claude (40-min build) ballpark.

**Recommended next moves (in order):**

1. **Make Decomposer opt-in, not opt-out.** Default `v2_build`
   to `--no-decompose` until we have an issue class where it
   demonstrably wins. The current "always on" default has cost us
   3 days on recipe_v2.
2. **Add a second A/B issue family.** Pick something the fat
   dispatch fails on (a known-hard greenfield issue), re-run.
   If decomposed wins there, we keep it as a tier-2 strategy
   gated on first-pass failure rather than always-on.
3. **Decomposer prompt tightening.** If we keep it, the per-unit
   p50 of 179s with zero retries is suspicious — the model is
   probably re-reading the same files 7 times. The decomposer's
   unit prompts could include the previous unit's exact diff
   instead of letting Coder re-discover the workspace each time.

## Run metadata

- **Git rev:** `b955e729`
- **Date:** 2026-05-18
- **Model:** claude-cli (default tier 0) on both sides
- **Fixture:** identical workspace_seed copied for each side
- **Tags:** `perf/coder_be006_fat/run-1`, `perf/coder_be006_decomposed/run-1`
- **Result files:**
  - `~/bizniz_perf_tests/coder_be006_fat/.runs/20260518_161715/result.json`
  - `~/bizniz_perf_tests/coder_be006_decomposed/.runs/20260518_162211/result.json`
- **Validation:** `python -m bizniz.perf_tests validate <slug>/<run>`
  augments any existing run's result.json with a `quality` block
  (AST parse + symbol resolution). Run live during scenarios going
  forward; applied post-hoc for both runs above.
