# coder_ba_fix2_2 — Decomposer on a complex multi-file issue

**Question:** when bare fat dispatch faces an issue we believed would
trip it up, does Decomposer earn its keep?

**Fixture:** ``BA-fix2-2`` lifted from recipe_v2's M3 (coder_issues
row id=203). Title: *"Wire selectinload + extend routes for tags /
search / filter."* 10,143-char description. Two files
(``app/repositories/recipes.py`` + ``app/api/routes/recipes.py``).
17 success criteria including a load-bearing cross-user-tag-side-
effect security property. Workspace seed = recipe_v2 at ``m2-done``
git tag (M2 finished, M3 hasn't started — strictly harder than what
production gave the Coder, since prereqs like the Tag model and tag
repository **don't exist** in the seed and must be created from
scratch).

**Tests (three-way):**
- ``bizniz/perf_tests/tests/coder_ba_fix2_2_fat.py`` — single Coder
  dispatch for the full scope. Decomposer not in the loop.
- ``bizniz/perf_tests/tests/coder_ba_fix2_2_decomposed.py`` —
  Decomposer runs at runtime, each emitted UnitOfWork wrapped via
  the production ``_unit_to_coder_issue`` shim, dispatched serially.
- ``bizniz/perf_tests/tests/coder_ba_fix2_2_fat_with_guideline.py``
  — Decomposer runs first, unit list appended as advisory markdown
  to the parent description, single fat dispatch consumes it.

All three share one ``workspace_seed`` (relative symlink chain to
the ``coder_ba_fix2_2_fat`` fixture). Validated via the runner's
``fixture_sha256`` env fingerprint.

## Results

| | bare fat | decomposed | guideline-fat (incomplete) |
|---|---|---|---|
| **wall clock** | **1074s** (17m 54s) | **4136s** (68m 56s) | **1130s+ killed at ~30m** |
| **Δ vs bare fat** | baseline | **+285%** | already > +5%, capped at +180% by 1800s subprocess timeout |
| decomposer cost | — | 65s | run never returned, no data |
| decomposer confidence | — | 0.88 | — |
| coder calls | 1 | 8 | 1 |
| coder elapsed | 1074s | 4071s | 1130s+ killed |
| median coder call | 1074s | **534s** | n/a |
| structural match | **14/14** | **14/14** | run terminated |
| AST parse | ok | ok | — |
| symbol validation | passed (13+16 resolved) | passed (7+17 resolved) | — |
| total output size | 35,414 B / 1,079 ln | 67,367 B / 1,624 ln | — |
| git rev | `0901e4c` | `b132642` | `b132642` |
| tags | `perf/coder_ba_fix2_2_fat/run-1` | `perf/coder_ba_fix2_2_decomposed/run-1` | (none — terminated) |

### Per-unit timings (decomposed, 8 units)

| unit | target | elapsed | status |
|---|---|---|---|
| BA-fix2-2-U1 | repositories/recipes.py | 118s | passed |
| BA-fix2-2-U2 | repositories/recipes.py | 534s | passed |
| **BA-fix2-2-U3** | repositories/recipes.py | **1093s** | passed |
| BA-fix2-2-U4 | api/routes/recipes.py | 212s | passed |
| BA-fix2-2-U5 | api/routes/recipes.py | 412s | passed |
| BA-fix2-2-U6 | api/routes/recipes.py | 593s | passed |
| BA-fix2-2-U7 | api/routes/recipes.py | 625s | passed |
| BA-fix2-2-U8 | api/routes/recipes.py | 484s | passed |

**One unit (U3) took as long as bare fat's entire run.** That's the
shape of the cost: per-call overhead is substantial, and slicing the
work doesn't divide that overhead.

## Verdict

### 1. The fat-fails hypothesis failed.

The whole reason for picking ``BA-fix2-2`` was that we expected it
to break bare fat: 10K-char description, multi-file scope, dense
prereq references, cross-user security property, prereqs missing
from the seed. **Bare fat shipped it in 17 minutes**, tier 0, zero
retries, AST-clean, symbol-clean, 14/14 structural markers, with
its own self-reported summary explicitly calling out the cross-user
guard. The Coder built the prereqs, did the wiring, wrote the
tests, all in one shot.

That's now **two fixtures (BE-006 + BA-fix2-2)** where bare fat
wins decisively. We do not yet have a fat-fails fixture.

### 2. Decomposer has no path to value on either fixture.

| mode | BE-006 (easy) | BA-fix2-2 (hard) |
|---|---|---|
| bare fat | 296s (1×) | 1074s (1×) |
| guideline-fat | 514s (1.74×) | ≥1130s (≥1.05×, killed at ~30m) |
| decomposed | 1196s (4.04×) | 4136s (3.85×) |

Same direction both times. Decomposer mode never wins on speed, and
never wins on quality — both modes hit the same 14/14 structural
markers + AST clean + symbol clean as bare fat. Decomposed produces
**91% more code** for no functional gain (ceremony per unit that
the bare-fat dispatch unified).

### 3. The 30-50x recipe_v2 regression is a Decomposer artifact.

Production recipe_v2 M3 had 15 errored issues, but **13 were
Anthropic 429 rate-limit failures** and 2 were 1800s subprocess
timeouts. The Coder never got to try the content. When we run the
same issue in isolation (no 5h usage-cap window pressure, no
back-to-back dispatch density), bare fat handles it cleanly.

The regression isn't because Coder needs help on complex issues.
It's because:
1. Decomposer multiplies dispatches by ~7-8× per issue, and each
   dispatch has ~3 min of overhead (cold start + workspace re-walk
   + context re-build). That overhead dominates per-token cost.
2. The denser dispatch pattern hits Anthropic's rate-limit windows
   sooner, which then errors-out subsequent issues for a full hour.

Both effects compound. Fat dispatch sidesteps both.

### 4. What we still **don't** know

- Whether Decomposer wins on a different model tier (Gemini-flash,
  smaller Claude variants, GPT-class). The original "reliability on
  complex issues" rationale may have been valid for those — we just
  don't have data.
- Whether there is ANY issue class where Decomposer helps on Claude
  CLI. Two fixtures isn't enough to conclude "never," but it is
  enough to conclude "not the default."

## Tier-strategy follow-up: Haiku bare fat (2026-05-18)

After the three-way verdict landed, ran a fourth scenario
(``coder_ba_fix2_2_fat_haiku``) — identical to bare fat but pinned
to ``--model=claude-haiku-4-5``. Same fixture seed, same workspace
state, only the model knob changed.

| | Opus fat | **Haiku fat** | Δ |
|---|---|---|---|
| wall clock | 1074s | **628s** | **-42%** |
| structural | 14/14 | **14/14** | same |
| AST parse | ok | ok | same |
| symbol validation | passed (13+16) | passed (8+16) | same |
| target files written | 2 | 2 | same |
| **test file written** | 0 (not in target_files) | **1** (`tests/integration/test_recipes_tags_search.py`) | **+1** |
| coder iters | 0 | 0 | same |
| tier used | 0 | 0 | same |
| self-reported security guard | yes | yes ("load-bearing security properties") | same |

Haiku didn't just hit parity — it **beat Opus by 42% wall** AND
wrote the integration test that Opus's bare-fat skipped. Tier 0,
zero retries, AST-clean, symbol-clean. On Anthropic's metered
billing Haiku is ~4-5× cheaper per token, so the combined effect
on a recipe_v2-class build is roughly **7-9× cheaper end-to-end**
(faster wall + cheaper rate). Tag: ``perf/coder_ba_fix2_2_fat_haiku/run-1``,
rev ``39eb8dd``.

This locks the tier strategy: **default Coder/Tester/Debugger to
Haiku, escalate to Opus only on stall.** Planning agents
(Architect, Planner, ServicePlanner, AuthPlanner, QE, CR) stay on
Opus where reasoning depth matters more than per-call cost.

## Implications

### Flip Decomposer to opt-in. Default ``v2_build`` to ``--no-decompose``.

Two-fixture evidence is strong enough. Keep Decomposer in the
codebase for the model-tier experiments and as a tier-2 fallback
strategy if/when we find an issue class where it earns its keep.
But ``v2_build --decompose`` becomes the rare opt-in path, not the
default. Cost-of-bug: roughly halves recipe_v2-class wall clock and
sidesteps the rate-limit cascade.

### Don't reshape Decomposer into a Planner/Framer (yet).

Earlier I floated the idea of a Decomposer that explores the
codebase first and emits a structured briefing instead of a unit
list. With bare fat performing as well as it does, that ticket
goes back into the freezer until we have a fixture where bare fat
genuinely struggles.

### The next fat-fails hunt is lower-priority.

We've sunk meaningful time looking for a fat-fails fixture and
haven't found one. The next-best signal probably lives in the
``coverage gap`` failure mode (Coder claims "passed" but skipped
writing requested tests) rather than in raw issue complexity.
That's a different kind of test fixture — one that checks
*test-file presence* + *test scenarios covered*, not just produced
code shape.

## Run metadata

- **Date:** 2026-05-18
- **Model:** ``claude-cli`` (default tier 0) on all sides
- **Fixture seed:** recipe_v2 at git tag ``m2-done``; same bytes
  across all three sides via shared workspace_seed.
- **Tags + git rev:**
  - `perf/coder_ba_fix2_2_fat/run-1`           — rev `0901e4c`
  - `perf/coder_ba_fix2_2_decomposed/run-1`    — rev `b132642`
  - guideline-fat: terminated at ~30m wall, no tag.
  - `perf/coder_ba_fix2_2_fat_haiku/run-1`     — rev `39eb8dd`
- **Result files:**
  - `~/bizniz_perf_tests/coder_ba_fix2_2_fat/.runs/20260518_173853/result.json`
  - `~/bizniz_perf_tests/coder_ba_fix2_2_decomposed/.runs/20260518_175647/result.json`
  - `~/bizniz_perf_tests/coder_ba_fix2_2_fat_haiku/.runs/20260518_193652/result.json`
- **Validation:** quality block (AST + symbol-validator) ran live
  during the scenarios.
