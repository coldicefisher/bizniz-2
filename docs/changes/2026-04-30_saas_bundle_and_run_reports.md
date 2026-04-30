# 2026-04-30 (evening) — SaaS skeleton bundle, per-run reports, template rename-resilience

Major session shipping the production-shaped saas skeleton bundle, the
per-run efficiency report system, end-to-end recovery tests for the two
pipeline bugs from 2026-04-29, and a string of template-rename bugs
caught by heavy AI smoke runs.

## 1. `bizniz-skeleton-saas` — production-shaped 4-service bundle (new repo)

A new opinionated skeleton bundle: when the architect sees a SaaS-shaped
problem (real-time updates, long-running jobs, OAuth login, profiles),
it picks four `saas-*` skeletons together with the `postgres`, `redis`,
and `fusionauth` infrastructure templates. Confirmed against real Gemini.

```
bizniz-skeleton-saas/
├── api/                FastAPI + FusionAuth JWT + OAuth callback +
│                       profile auto-create + Article CRUD +
│                       /regenerate (dispatches Redis Streams job)
├── websocket-server/   FastAPI WS, JWT validate on connect,
│                       Redis Pub/Sub bridge, room join/leave
├── store-consumer/     Redis Streams worker. Sample job
│                       (regenerate_article) that acquires a
│                       processing lock + WS broadcast, sleeps 5s,
│                       releases lock + broadcast.
├── frontend/           Angular 19 SPA. OAuth flow, JWT interceptor,
│                       EntityChannelService for live entity events
│                       (mirrors MUSE realtime pattern).
└── core/               Vendored shared package (auth/store/locks/
                        websocket/db) — sync_core.sh keeps each service
                        in sync with the canonical copy at repo root.
```

Test totals on the skeleton:
  - api: 38 unit + 1 integration
  - store-consumer: 7 unit
  - websocket-server: 17 unit
  - tests/e2e: 1 docker-required smoke
  - **64 tests; 63 fast (<1s), 1 docker-required**

Registered in `bizniz/architect/skeletons.py` as `saas-api`, `saas-ws`,
`saas-consumer`, `saas-frontend`. Pushed to
`github.com/coldicefisher/bizniz-skeleton-saas`.

Reference: `bizniz-skeleton-saas/README.md` for architecture diagram.

## 2. Per-run efficiency reports (commit `02ecb74`)

`Architect.build()` now writes two files to `<project_root>/docs/runs/`:

  - `<job_id>.md`    — human-readable: header, architecture table,
                       models snapshot from BiznizConfig, per-service
                       results, cost roll-up by model + agent, and
                       (when a prior run exists) a "delta since last
                       run" block with arrows showing direction.
  - `<job_id>.json`  — machine-readable. The next run reads this to
                       compute Δ. Stable across markdown format changes.

Wired from the `Architect.build()` finally-block, wrapped in try/except
so report failures never crash the run. When the build fails before
project_root materializes, no report is written.

11 unit tests. Doc: `docs/architecture/run_reports.md`.

## 3. End-to-end recovery tests for pipeline bugs 1 + 2 (commit `bedd1c0`)

The classifier (`_is_source_import_error`) and config allowlist
(`_is_config_file`) had unit tests. What was missing:
**orchestrator-loop tests** that drive `run_multi` and confirm the right
recovery branch fires.

`bizniz/orchestrator/tests/test_pipeline_bug_recovery.py`:

  - **Bug 1 positive** — pytest exit-code-2 with a `NameError: FastAPI`
    traceback pointing at our source routes to `coder.repair_multi`,
    NOT `tester.generate_multi`. The repair prompt mentions SOURCE CODE.
  - **Bug 1 negative** — fixture-not-found in test still routes to test
    regeneration. Guards against over-aggressive classifier.
  - **Bug 2 positive** — `jest.config.js` is on disk but not in
    `target_files`; failing test points at it. The orchestrator hands
    it to the coder as **writable** (via `_CONFIG_FILENAMES`) AND the
    repair survives the read-only filter (verified via
    `workspace.write_file`).
  - **Bug 2 negative** — non-config file from a prior issue (NOT on the
    config allowlist) is still filtered. The fix isn't a free-for-all.

Both bugs are now belt-and-suspenders protected: classifier-level unit
tests + orchestrator-loop recovery tests. See
`memory/project_pipeline_bugs.md` for shipped status.

## 4. Provisioner template hostname rename-resilience (commits `c7effe5`, `1ee69bf`, `70246f6`)

Three template bugs in the same family — surfaced by heavy AI smoke runs
because real Gemini picks different service names every run.

  - **FusionAuth template** had `depends_on: postgres` and
    `DATABASE_URL=jdbc:postgresql://postgres:5432/...` hardcoded. The
    architect named postgres `db` → compose rejected with *"service
    'auth' depends on undefined service 'postgres'"*.
  - **Postgres template** emitted
    `DATABASE_URL=postgresql+asyncpg://dev:dev@postgres:5432/...`. Same
    rename problem — api containers crashed at startup with
    `socket.gaierror: Name or service not known`.
  - **Redis template** emitted only `REDIS_URL=redis://redis:6379/0`.
    Skeleton's `RedisConfig` reads `REDIS_HOST` (not `REDIS_URL`) by
    default — falls back to "redis" when env var unset.

**Fix pattern**: `TemplateContext` now carries `architecture` reference
plus a `find_by_framework("postgres")` helper. Templates resolve sibling
service names dynamically via the helper or `ctx.service.name` for
self-references. Redis template also emits `REDIS_HOST` + `REDIS_PORT`
so skeletons that read either form find the right hostname.

Regression tests: `test_compose_depends_on_renamed_postgres_service`,
`test_database_url_uses_actual_service_name_not_hardcoded_postgres`,
`test_redis_url_uses_actual_service_name`,
`test_redis_emits_host_and_port_env_vars`.

## 5. Compose generation fixes (commits `c4ade37`, `867f820`, `58776f8`)

Five additional compose-builder bugs caught by smoke tests, all fixed:

  - **Compose `dockerfile:` path** is relative to the build context,
    not the compose file. Was off by one `..`. Fixed: `../../infra/...`
    → `../infra/...`.
  - **Compose project name collision**: every bizniz project at
    `infra/development/docker-compose.yml` got compose-project-name
    `development`. Two projects on the same machine clobbered each
    other (vehinexa got stomped during a smoke run). Fixed: emit
    `name: <slug>` at the top of compose YAML so each project is
    isolated.
  - **Node services lost `node_modules`** — the workspace bind-mount
    masked the npm-installed deps, `npm run dev` failed with `vite:
    not found`. Fixed: anonymous volume on `/app/node_modules` for
    `typescript` / `javascript` services.
  - **Compose `image:` field added** so `docker compose up` reuses the
    Provisioner-built image instead of trying to rebuild every time.
  - **Skeleton `container_port` overrides framework default** in
    `_container_port_for`. saas-frontend (angular, container 5173)
    was being mapped to angular's framework-default 4200; nothing
    listened. Fixed: skeleton's `container_port` wins.

## 6. FusionAuth kickstart fix (commit `867f820`)

`defaultTenantId` set to FusionAuth's built-in default UUID
(`00000000-0000-0000-0000-000000000000`) triggered an internal
"rename the default tenant" operation that failed with a
`tenants_pkey` unique-constraint violation. Removed the variable;
PATCH URL now references the UUID literally.

## 7. Smoke test infrastructure

Three smoke tests now live in `bizniz/provisioner/tests/functional/`:

  - **`test_full_stack_smoke_no_ai.py`** — hand-crafted CRM
    architecture, no AI tokens, ~30s. Suitable for CI / PR gating.
  - **`test_full_stack_smoke.py`** — CRM problem statement against
    real Gemini, ~35s with caches warm. ~$0.20 / run.
  - **`test_saas_stack_smoke.py`** — SaaS-shaped prompt against real
    Gemini, asserts the architect picks `saas-*` skeletons and the
    full 7-service stack comes up. ~2-5 min, ~$0.20 / run.

Shared helpers in `_smoke_helpers.py`. All three pass as of this commit.

## Insight: heavy AI smoke as bug-catcher

A pattern noticed across this session: heavy AI smoke runs catch a
class of bug that mocked unit tests can't, because the architect picks
different service names every run. Three template-hostname bugs surfaced
this way (FusionAuth, postgres, redis). The fix-and-test cycle for each
was: real run → reproduce → fix template + add unit test that exercises
the rename → re-run.

If you ship a new infrastructure template, add a regression test that
constructs the template with a non-default `service.name` (e.g. `data`
instead of `postgres`) and asserts every emitted hostname/depends_on
uses that name.

## Test totals after this session

  - bizniz unit suite: **668** (was ~624 at start of session, +44)
  - bizniz-skeleton-saas: **64** (entirely new)
  - Smoke tests: 3 — all pass
  - Heavy AI smoke for both CRM and SaaS shapes: PASSED end-to-end

## Pushed commits

`bizniz`:
  - `c4ade37` — Free no-AI smoke test + compose dockerfile path fix
  - `867f820` — 4 fixes: compose project name, asyncpg, node_modules,
                FA kickstart
  - `58776f8` — compose_builder respects skeleton container_port
  - `c7effe5` — FusionAuth template resolves postgres name dynamically
  - `02ecb74` — Per-run efficiency reports
  - `bedd1c0` — End-to-end recovery tests for pipeline bugs 1+2
  - `1ee69bf` — postgres + redis templates use service.name
  - `70246f6` — redis template emits REDIS_HOST + REDIS_PORT

`bizniz-skeleton-saas` (new repo):
  - `4473bf8` — Initial: core + 4 services + frontend
  - `5060c19` — Frontend Dockerfile path fix
  - `5ab2013` — Tests: 38 unit + 1 integration + 1 e2e
  - `adaf358` — Tests for store-consumer + websocket-server (24 more)
