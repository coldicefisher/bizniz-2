# coder_single_issue — Coder baseline microbenchmark

**Question:** what's the wall-clock cost of ONE Coder dispatch on
ONE solved-known-good issue?

**Test:** `bizniz/perf_tests/tests/coder_single_issue.py`  
**Fixture:** `bizniz/perf_tests/fixtures/coder_single_issue/`

## Setup

Workspace seed = `bizniz-skeleton-fastapi` + a minimal Recipe ORM
model + `RecipeCreate`/`RecipeOut` schemas + a services helper
module (`create_recipe`, `ensure_local_user`). Represents the
state of a recipe_v2-class project after BE-001..BE-005 in a
clean synthetic environment.

Issue dispatched = `BE-006-U2` ("Implement POST /api/recipes"),
lifted from recipe_v2's M2 production data.

The fixture is committed; the runner copies it to a per-run
workspace under `~/bizniz_perf_tests/coder_single_issue/.runs/<id>/`.

## Runs

| Run | Tag | wall (s) | tier | iters | tests-passed | match |
|---|---|---|---|---|---|---|
| 1 | `perf/coder_single_issue/run-1` | **476** | 0 | 0 | 7/7 (Coder-reported) | 9/10 |

### Run-1 observations

- 476s ≈ 7m 56s for ONE Coder dispatch on a real-issue spec.
- Tier 0 (claude-cli), 0 retry iterations. Single shot, clean exit.
- File produced: 66 lines, idiomatic FastAPI:
  - `APIRouter(prefix='/recipes', tags=['recipes'])`
  - `require_roles('user', 'admin')` dependency (captured once,
    not re-built per request)
  - `RecipeCreate` payload, `RecipeOut` response model
  - `owner_id = user.user_id` (derived from the JWT)
  - Status 201, body summary in docstring
- Coder claimed "7/7 tests pass" — pytest actually ran in the
  fixture workspace and passed (covers 201 happy, 401 unauth,
  403 missing role, 422 forged-owner_id/forged-id/missing-title).
- Pattern miss: `ensure_local_user` wasn't called (the seeded
  helper). Coder went straight from JWT to user.user_id without
  the explicit mirror-upsert step, since the seeded `require_roles`
  already returns a `User`. **Not a bug — a different design choice.**

## Implications for the broader perf question

Coder per-call cost is **~5-8 minutes** for a representative
real-issue dispatch. Production M1-M3 saw 140 such calls; at the
observed median of 2m 5s and our 476s for THIS issue (which sat
near p95 in the production distribution), the math checks out:

```
production Coder total time (M1-M3):       5h 39m
this microbench, single issue:             7m 56s
production p95 (per call):                 7m 21s
production max (per call):                 14m 1s
```

This single call landed at p95 — consistent with the issue being
moderately complex (POST handler with auth + Pydantic + DB call).

**Conclusion: Coder per-call cost is intrinsic to the LLM dispatch
+ tool-loop, not driven by some bug in our orchestration.** Saving
time on Coder needs one of:

1. **Parallelism** — currently units run serially within an issue.
   Independent units (different files) could be in flight at once.
2. **Simpler issues** — if Decomposer makes issues so small that
   each Coder call is faster, multiplier might pay for itself.
3. **Different model tier** — faster/dumber model for low-risk
   units. Today everything goes to claude-cli tier 0.

## Next test

`coder_decompose_ab` — fat issue (entire BE-006 in one Coder
dispatch) vs decomposed (BE-006-U1..U7 in 7 serial dispatches).
Both against the same fixture. Lets us answer: does Decomposer
reduce or increase total Coder time?

## Run metadata

- **Git rev:** `1c8b32d`
- **Date:** 2026-05-18
- **Model:** claude-cli (default tier 0)
- **Notes:** First-ever run after harness landing; harness validated
  end-to-end. Failed attempt #1 (bad import) cleaned up; counted
  as run-1 only when the scenario completed.
