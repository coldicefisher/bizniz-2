# 2026-05-01 — Pipeline completion session

This session shipped the full backend + frontend integration phase
end-to-end and ran 10 verification iterations of the pet-groomer
prompt to find every gap. By session end, the pipeline can take a
problem statement to a Dockerized multi-service app, write
integration tests against the live stack, and surface real bugs
honestly. The integration debugger has one remaining regression
fixed in `ea6aa38`; V11 should verify it.

## Headline outcomes

- **Build pipeline reliable for greenfield**: every V8/V9/V10 run
  finished engineering with all unit tests passing; skeleton paths
  obeyed; contract handoff between layers working; no silent dead
  code; honest cost reporting.
- **Integration phase live**: HTTPApiTester (pytest+httpx in a
  python sidecar) and WebUITester (Playwright in a Microsoft
  Playwright sidecar joined to the compose network) write real
  tests, run against the live stack, and fail loudly when domain
  coverage is missing.
- **AgenticDebugger wired** for both backend and frontend integration
  failures. Up to 3 outer iterations × 15 tool turns each. As of V10
  it crashed on a `_ai_client` regression — fixed in `ea6aa38`,
  unverified end-to-end.
- **Architect prompt strict**: V8+ only adds infrastructure
  (database, auth, cache, queue, etc.) that the prompt explicitly
  asks for. No more "real apps need auth" drift.

## What landed in this session, in chronological order

### Phase 1 — Skeleton conventions (V4-V5 era)

Three layers of defense against the "engineer writes parallel package
nobody reaches" bug class (V3 false-green dark domain):

1. **Skeleton SKELETON.md contracts** — each of the 5 skeletons ships
   directory rules. `app/api/routes/*.py` for FastAPI, `src/pages/`
   + `src/routes/` for React, etc.
2. **Engineer prompt threading** — `load_skeleton_conventions()`
   reads SKELETON.md from the workspace and injects into both system
   prompt AND user prompts (analyze + plan). The user-prompt
   injection is critical; system-prompt-only got overridden by the
   default "design a Python package" instruction.
3. **Smoke verifier** — replaced by the integration phase below.

### Phase 2 — Integration phase v1 (V6-V7)

- `bizniz/integration/contracts.py` — capture OpenAPI from each
  backend mid-build and at verify time. Saves to
  `<project>/contracts/<svc>.openapi.json`.
- `bizniz/integration/http_api_tester.py` — `HTTPApiTester` agent.
  Reads problem statement + service def + captured OpenAPI, writes
  pytest+httpx test file with domain-noun coverage requirement.
- `bizniz/integration/runner.py` — orchestrates: full stack up,
  capture contracts, dispatch tester, run pytest in a sidecar
  (`python:3.12-slim` + `pip install pytest httpx`) joined to the
  compose network. `--noconftest --rootdir tests/integration` so
  the project's own conftest doesn't try to load (sqlalchemy etc.).

### Phase 3 — Skeleton fixes from real failures (V7-V8)

- **V7 surfaced**: backend container exited because the skeleton's
  lifespan called `Base.metadata.create_all` against a Postgres
  that wasn't reachable.
- **Fix**: skeleton lifespan now wrapped in try/except. Dev-friendly:
  if DB is unreachable, log warn and continue; routes that need DB
  fail at request time. Production should swap to Alembic.
- **Runner gap caught**: when the backend container never starts,
  contract capture returns None — runner just marked failed without
  engaging the debugger. Fixed by capturing docker logs and
  dispatching the debugger with those as failure_output, then
  retrying via `_retry_backend_health` (compose restart + poll
  /openapi.json).

### Phase 4 — WebUITester (V9-V10)

- V9 surfaced: pipeline reported "succeeded" but standing the
  artifact up showed a blank page (`describe is not defined`)
  because the engineer placed a Jest test file in `src/routes/`
  and Vite's auto-discovery glob eagerly imported it.
- **Skeleton fix**: glob now excludes `*.test.tsx`/`*.spec.tsx`,
  accepts `default` as array OR single object, console.warn loudly
  on neither.
- **WebUITester** built: `bizniz/integration/web_ui_tester.py`. AI
  emits `.spec.cjs` files (CommonJS — sidesteps ESM/TS loader
  friction when @playwright/test installs into a Vite workspace).
  Sidecar: `mcr.microsoft.com/playwright:v1.40.0-focal`, joined to
  compose network, runs against `http://frontend:5173`.
- **Vite host fix**: skeleton's `vite.config.ts` now sets
  `allowedHosts: true` so docker-compose internal hostnames work
  for Playwright + sibling-container requests.
- **Runner sidecar bugs found and fixed during smoke testing
  against V9** (no AI cost — hand-written .cjs tests):
  - heredoc EOF terminator on same line as `&&` swallowed rest of
    script as heredoc body. Replaced with `printf`.
  - workspace must use `.cjs` config (.js → ESM strict mode breaks
    `module.exports`; .ts → Node can't load).
  - Test files emit as `.spec.cjs` with `require()` syntax.

### Phase 5 — V10 ran end-to-end (this session's culmination)

V10 ran the entire pipeline including both integration test types
for the first time. Backend + frontend both engineered cleanly,
file paths perfect, contract handoff worked. **Both integration
test suites failed** — surfaced real bugs in the AI's generated
app:

**Backend (9 passed / 2 failed):**
- `POST /api/v1/appointments` returned 400 instead of 201/422 — request
  shape mismatch with what the AI declared in OpenAPI.
- `test_missing_view_appointments_endpoint` deliberately failed —
  HTTPApiTester noticed the prompt mentions "view their existing
  appointments" but the spec has no list endpoint, only single-by-id.

**Frontend (0 passed / 6 failed):**
- Home page renders only skeleton placeholder, not domain content
- Navigation between services/booking/appointments doesn't work
- API calls from frontend hit backend 500s
- All real failures, all caught by Playwright assertions

**AgenticDebugger crashed** on `'AgenticDebugger' object has no
attribute '_ai_client'` — regression from earlier per-call
attribution fix; debuggers inherit from `BaseDebugger` not
`BaseAIAgent`. Property added to `BaseDebugger` in `ea6aa38`,
unverified end-to-end.

## Final ledger

**6 repos, all on main, all pushed:**
- `bizniz` — ea6aa38 (BaseDebugger fix is HEAD)
- `bizniz-skeleton-fastapi` — auto-discovery loud + lifespan tolerant + SKELETON.md
- `bizniz-skeleton-react` — auto-discovery permissive + loud + .test exclusion + vite allowedHosts + SKELETON.md
- `bizniz-skeleton-angular` — SKELETON.md
- `bizniz-skeleton-teams` — SKELETON.md
- `bizniz-skeleton-saas` — SKELETON.md

**76 unit tests in bizniz, all green.**

## What V11 should verify

Run with the BaseDebugger fix in place. Expected:

1. Same pipeline shape as V10 (2 services, in-memory).
2. Backend integration: 11 tests authored. Either passes (engineer
   nails it this time) or fails again. **If fails**, AgenticDebugger
   should now actually run — read the diagnose output for evidence
   that tools are being called.
3. Frontend integration: 6 tests authored. Same expectation.
4. Cost target: $0.60-0.80 if no debugging, $1.50-2.50 if debugger
   exhausts iterations.

## Known remaining issues (for V11+ to surface)

- Engineer still co-locates tests (`src/pages/Foo.test.tsx`) despite
  SKELETON.md forbidding it. Skeleton's loud-fail glob protects
  against the runtime crash, but the SKELETON.md guidance isn't
  fully landing.
- Frontend domain coverage is weak — engineer builds API clients
  and pages but doesn't wire them into actual user flows.
- The HomePage placeholder is never replaced with domain content.
  This is something the engineer's prompt could be more explicit
  about: "the React skeleton's HomePage.tsx is a placeholder; you
  MUST replace its content with the domain home page."

## Strategy alignment

This session executes against the **build vs evolve** plan
(`docs/changes/2026-05-01_build_vs_evolve_strategy.md`):
greenfield mode is now reliable enough that the four v0 artifacts
(architecture digest, integration tests, SKELETON.md, OpenAPI
contracts) are produced cleanly per run. **Evolve mode is
unbuilt** — the next major work is implementing
`architect.evolve()` to read those artifacts and add features to
already-built apps using the discovery toolkit
(`bizniz/tools/discovery_tools.py`) the AgenticDebugger already
uses.

## How to resume

1. Read `CLAUDE.md` at the repo root.
2. Read `docs/memory/MEMORY.md` for the index of project memories.
3. Run V11: `cd ~/bizniz && set -a && source .env && set +a &&
   PYTHONPATH=. .venv/bin/python -u examples/auto_architect.py`
4. The project_name in `examples/auto_architect.py` will need
   bumping to V11 first (currently V10).
