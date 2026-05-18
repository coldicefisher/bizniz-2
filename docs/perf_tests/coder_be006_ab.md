# coder_be006_ab — Decomposer cost-benefit on BE-006

**Question:** does Decomposer make Coder faster, slower, or the same
on a real CRUD-router issue? And does it cost or gain quality?

**Tests (three-way):**
- `bizniz/perf_tests/tests/coder_be006_fat.py` — single Coder dispatch
  for the entire BE-006 scope (POST + GET list + GET one + PUT + DELETE +
  UUID coercion). **Decomposer not in the loop.**
- `bizniz/perf_tests/tests/coder_be006_decomposed.py` — 7 serial Coder
  dispatches for the same scope, one per `BE-006-U1..U7` unit.
  **Decomposer-as-dispatcher.**
- `bizniz/perf_tests/tests/coder_be006_fat_with_guideline.py` —
  Decomposer runs first, its unit list is appended to the issue as an
  "advisory, not literal" breakdown, then ONE fat Coder dispatch
  consumes the augmented issue. **Decomposer-as-guideline.**

All three run against the same shared workspace seed (recipe_v2-class
skeleton + Recipe model + schemas + helpers). The guideline-fat
fixture symlinks its `workspace_seed` to fat's so the seed bytes are
provably identical.

## Results — three-way, first run each

| | bare fat | guideline-fat | decomposed |
|---|---|---|---|
| **wall clock** | **296.4s** (4m 56s) | **514.3s** (8m 34s) | **1196.3s** (19m 56s) |
| **Δ vs bare fat** | baseline | **+74%** | **+304%** |
| decomposer | — | 33.9s | n/a (embedded) |
| decomposer confidence | — | 0.82 | n/a |
| coder calls | 1 | 1 | 7 |
| coder elapsed | 296s | 480s | 1196s |
| median coder call | 296s | 480s | 179s |
| structural match | 12/12 | 12/12 | 12/12 |
| AST parse | ok | ok | ok |
| symbol validation | passed (12 resolved) | passed (13 resolved) | passed (12 resolved) |
| file size | 6640 B / 190 ln | 7233 B / 206 ln | 8249 B / 219 ln |
| top-level defs | 9 | 9 | 8 |
| git rev (fat, dec) | `b955e729` | — | `b955e729` |
| git rev (guideline) | — | `e04d549` | — |
| tags | `perf/coder_be006_fat/run-1` | `perf/coder_be006_fat_with_guideline/run-1` | `perf/coder_be006_decomposed/run-1` |

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

### 1. **Pure decomposition is net-negative on BE-006-class issues.**

Both Decomposer modes (as-dispatcher AND as-guideline) lose to bare
fat. Guideline-fat — which was supposed to be the cheap variant —
added **74% wall clock** for zero quality gain. The Coder didn't
spend 26s extra (the Decomposer cost); it spent **180s extra**
inside its own dispatch. The guideline either pulled the Coder into
more elaborate code OR triggered extra tool calls / verification per
unit. Either way, the planning aid wasn't an aid.

Decomposed-as-dispatcher is even worse — 7 dispatches × ~3 min each
(cold start + workspace re-discovery + context re-build × 7).

### 2. Quality is **at parity** across all three modes.

- All three parse cleanly via `ast.parse`.
- All three pass `bizniz.coder.symbol_validator` with zero
  unresolved symbols, zero hallucinated attributes, zero syntax
  errors. (Guideline added 1 extra resolved symbol — an extra helper
  function — but no functional improvement.)
- All three match all 12 structural patterns.
- File sizes scale with the Decomposer's involvement: bare fat is
  tightest (190 ln), guideline-fat is middle (206 ln), decomposed is
  longest (219 ln). More words for the same shape.

### 3. Projection to recipe_v2 M2 (24 issues, IMPLEMENT phase)

```
bare-fat       projection: 24 issues × 4m 56s          ≈ 1h 58m
guideline-fat  projection: 24 issues × 8m 34s          ≈ 3h 25m
decomposed     projection: 80 units  × 3m 0s           ≈ 4h 00m
recipe_v2 M2   observed IMPLEMENT                       ≈ 3h 10m
```

Observed M2 lands between guideline-fat and decomposed projections —
consistent with the v2_build default being "always decompose" but
some issues having very few units.

### 4. What we still **don't** know

- Whether any Decomposer mode wins on issues the bare-fat dispatch
  actually **fails** on. The whole stated purpose of Decomposer was
  reliability on complex issues. BE-006 is moderately-complex CRUD
  that fat handles first-try, AST-clean, symbol-clean.
- Whether a richer "Planner / Framer" Decomposer (one that actually
  explores the codebase and emits a structured briefing — not just a
  unit list) would help where pure decomposition hurts.
- Per-run variance. One sample each. Could be ±60s of noise.

## Implications for the broader perf question

This is the first hard data point on what's driving the 30-50x
recipe_v2 regression. The math fits:

```
recipe_v2 M2 observed IMPLEMENT:  3h 10m
bare-fat projection (24 issues):  ≈ 1h 58m  ← bookshelf_claude territory
guideline-fat projection:         ≈ 3h 25m  ← in the observed range
decomposed projection (80 units): ≈ 4h 00m  ← in the observed range
```

**Both** Decomposer modes account for the regression. Bare fat would
have shaved roughly half the wall clock — at parity quality, on at
least this class of issue.

**Recommended next moves (in order):**

1. **Make Decomposer opt-in, not opt-out.** Default `v2_build`
   to `--no-decompose` until we have an issue class where some
   Decomposer mode demonstrably wins. The current "always on"
   default has cost us 3 days on recipe_v2.
2. **Find a fat-fails issue.** Pull a known-complex issue from
   recipe_v2 M4's 27-defect stall, run it through bare fat first.
   If fat succeeds first-try, pick a harder one. The goal is to
   establish a fixture where Decomposer's stated value (reliability
   on complex issues) is actually testable.
3. **Three-way against the fat-fails fixture.** All three modes,
   same fixture. THEN we'll know whether Decomposer's role should
   be eliminated, rescoped to as-guideline, or rebuilt as a
   Planner/Framer that explores the codebase first.
4. **(Deferred.)** If even guideline-fat doesn't win on the
   fat-fails fixture, the answer may be that Decomposer's right
   shape is a *Planner/Framer*: tool-loop the codebase, emit a
   structured briefing (relevant files + integration points + risks
   + idiomatic patterns), and the Coder consumes that as pre-flight
   context instead of rediscovering. Out of scope until (2)+(3)
   tell us pure decomposition is dead.

## Run metadata

- **Date:** 2026-05-18
- **Model:** `claude-cli` (default tier 0) on all three sides
- **Fixture:** all three sides share the same workspace_seed bytes
  (guideline-fat's seed is a relative symlink to fat's). Validated
  via the runner's `fixture_sha256` env fingerprint.
- **Tags + git rev:**
  - `perf/coder_be006_fat/run-1`           — rev `b955e729`
  - `perf/coder_be006_decomposed/run-1`    — rev `b955e729`
  - `perf/coder_be006_fat_with_guideline/run-1` — rev `e04d549`
  (Guideline-fat's rev is later because the scenario didn't exist
  at `b955e729`. Workspace seed bytes are provably identical — see
  fixture_sha256 in each result.json's `env` block.)
- **Result files:**
  - `~/bizniz_perf_tests/coder_be006_fat/.runs/20260518_161715/result.json`
  - `~/bizniz_perf_tests/coder_be006_decomposed/.runs/20260518_162211/result.json`
  - `~/bizniz_perf_tests/coder_be006_fat_with_guideline/.runs/20260518_171420/result.json`
- **Validation:** `python -m bizniz.perf_tests validate <slug>/<run>`
  augments any existing run's result.json with a `quality` block
  (AST parse + symbol resolution). Applied post-hoc to fat +
  decomposed; live during the guideline-fat scenario.
