# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quickstart

This file orients a Claude session in the bizniz repo. Read this
first; it tells you what to load next.

## What bizniz is (one paragraph)

Bizniz is a multi-agent AI pipeline that takes a natural-language
problem statement and produces a working, Dockerized, multi-service
app. The pipeline: **Architect** decomposes → **Provisioner**
materializes (clones skeletons + emits compose) → **Engineer** per
service generates code via three-phase strategy → **Coder /
Tester / QuickDebugger** loop on each issue → **HTTPApiTester**
writes pytest+httpx integration tests → **WebUITester** writes
Playwright tests → both run against the live stack →
**AgenticDebugger** auto-repairs integration failures. End artifact:
a `~/bizniz_projects/<slug>/` directory with running code, tests,
SKELETON.md contracts, captured OpenAPI, and a per-run report.

## What's in flight (as of 2026-05-03)

- **V11 ran**: full pipeline end-to-end including AgenticDebugger.
  Backend 5/5 engineering, 9/11 integration tests passed.
  Frontend 4/4 engineering but no react skeleton (GitHub auth
  failure on auto-clone → fell back to generated boilerplate).
- **Debugger works**: three bugs found and fixed in the integration
  debug loop. The debugger now repairs integration failures in
  1 iteration ($0.05) instead of exhausting 3 and escalating.
  - Container restart after code fixes (commit `bd24e90`)
  - System prompt tuned for Docker context (commit `bd24e90`)
  - Server-side logs auto-tailed to debugger (commit `5ad097a`)
  - `inspect_container` tool for on-demand log/exec (commit `f11ba67`)
- **Standalone harness**: `examples/debug_integration.py` runs only
  the integration phase against an already-built project — no need
  to re-pay engineering cost while tuning the debugger.
- **Workspace filtering fixed**: `list_relative_files()` now prunes
  node_modules + framework caches (Angular, Astro, SvelteKit, Vue/Nuxt,
  Turbo, Parcel, etc.) at the walk level. V11 frontend: 527 → 27 files.
  Debug loop also sends manifests (package.json, requirements.txt)
  first, excludes lockfiles.
- **Milestone builds wired**: `build_with_plan` now runs integration
  tests after each milestone. `examples/milestone_build.py` supports
  `--plan-only`, `--milestone N`, `--resume` for human-gated flow.
- **Property Manager test**: first full-lifecycle test in
  `tests/e2e/property_manager/`. Real Postgres, JWT auth, two roles,
  4 domains. Exercises planner → evolve → engineer → integration →
  debugger across multiple milestones.
- **Stack validation**: provisioner now brings stack up + health
  checks all services before engineering starts. Infra debugger
  dispatched on failure (Dockerfile, compose, init.sql).
- **Image rebuild**: Docker images rebuilt after engineering so
  integration tests run against final code, not skeleton state.
  Integration repair uses `--build --force-recreate` (not restart).
- **Import tools**: `search_imports` and `list_all_imports` available
  to all agents (coder, tester, debugger). Full signatures + docstrings.
  Preflight "did you mean?" hints injected into repair prompts.
- **FusionAuth skeleton**: fastapi skeleton delegates all auth to
  FusionAuth. No local JWT minting or password hashing.
- **Case normalization**: `ServiceDefinition` normalizes service_type,
  framework, language to lowercase via Pydantic validator. Template
  lookup case-insensitive with aliases.
- **FusionAuth agent**: reads problem statement → extracts roles +
  tenancy model → creates roles/test users via FusionAuth API →
  writes AUTH_CONTRACT.md for engineers. Sequencing fix needed
  (must run while stack is up, before teardown).
- **Pre-built test sidecars**: `bizniz-test-pytest:latest` and
  `bizniz-test-playwright:latest` eliminate runtime pip/npm install.
  30-60s saved per test execution. Auto-built if missing.
- **Design systems**: React skeleton → Tailwind CSS v4, Angular
  skeleton → Angular Material (already had it, now documented).
- **Milestone-scoped integration**: integration tests receive
  `milestone.problem_slice`, not the full problem statement.
- **FusionAuth sequencing fixed**: stack stays up through FusionAuth
  agent, tears down after. No more "Connection refused."
- **UX Designer agent**: screenshots frontend views via Playwright
  sidecar → sends to Gemini vision for design evaluation → dispatches
  Coder to apply fixes → re-screenshots to verify. Pipeline placement:
  after image rebuild, before integration tests. Uses `gemini-flash`
  for the screenshot script and vision evaluation.
- **Gemini vision**: GeminiClient now supports `get_text_with_images()`
  for multimodal prompts. 19 unit + 4 functional tests.
- **Debugger cost fixes**: duplicate fix bail (same code_fixes twice →
  stop), error signatures persist across agentic debugger (no 3-failure
  re-confirm), repair history persists Phase 2 → Phase 3 (deduplicated),
  OrchestratorMaxIterationsError propagates instead of being swallowed.
- **Image capture**: `_compose_up_and_capture_images()` runs
  `docker compose up -d --build`, reads image names from
  `docker compose ps --format json`, stamps onto ServiceDefinition.
  Fixed root cause of M1 unit test failures (tests ran in generic
  bizniz-python-runner instead of service image).
- **FusionAuth auto-create**: agent now creates the application if
  missing (kickstart only runs on first boot; stale DB = missing app).
- **Config**: gemini-pro → gemini-flash-top across bizniz.yaml. Pro
  quota hit at 250/day; flash-top (gemini-3-flash-preview) has same
  vision support and no daily cap.
- **Auth contract in debugger**: integration debugger now loads
  AUTH_CONTRACT.md and injects it into the debug context. Tells the
  debugger that skeleton auth files MAY be modified to match the
  FusionAuth contract. Previously the debugger diagnosed auth
  mismatches correctly but couldn't fix skeleton-provided files.
- **UX designer screenshot waits**: 5s waitForTimeout after every
  navigation (was 1s), 30s goto timeout (was 15s), 180s subprocess
  timeout. SPAs need time for Vite HMR + React hydration.
- **M1 status**: engineering passes (backend 3/3, frontend 5/5).
  FusionAuth auto-create works (smoke PASS, valid JWT). UX designer
  runs and dispatches coder fixes. Integration auth failures are
  the last blocker — auth contract context fix is deployed but not
  yet validated (M1 currently running). Backend had 9/12 integration
  pass in prior run, 3 auth route mismatches.
- **Pending**: M1 is running now. If auth integration passes, run M2.
  If not, investigate the specific auth route mismatch — the debugger
  has the contract now, so it should be able to fix the skeleton's
  auth routes to match FusionAuth's actual behavior.

## Where things live

| What | Where |
|---|---|
| This repo (orchestration) | `~/bizniz/` |
| Generated apps | `~/bizniz_projects/<slug>/` |
| Per-run reports | `<project>/docs/runs/<job_id>.md` (and .json) |
| E2E lifecycle tests | `tests/e2e/` (property_manager is the first) |
| Skeleton repos (5) | `~/bizniz-skeleton-{fastapi,react,angular,teams,saas}/` |
| Auto-memory (this machine) | `~/.claude/projects/-home-jamey-bizniz/memory/` |
| Portable memory copy (this repo) | `docs/memory/` |
| Session narratives | `docs/changes/<date>_<topic>.md` |
| Strategy / plans | `docs/changes/2026-05-01_*.md` (pet-groomer, build-vs-evolve) |

## Code architecture

The `bizniz/` package is the orchestration engine. Key abstractions:

- **BaseAIClient** (`core/client.py`) — abstract interface for LLM calls.
  Implementations: `clients/chatgpt/` (OpenAI), `clients/claude/`,
  `clients/gemini/`. All return `(text, job_id, output_messages)`.
- **BaseAIAgent** (`core/agent.py`) — base for all agents. Holds a
  client, an execution environment, and a workspace. Manages message
  history and retries.
- **BaseWorkspace** (`workspace/base_workspace.py`) — file I/O abstraction.
  `LocalWorkspace` is the concrete impl. Each service gets its own workspace
  rooted at `<project>/<workspace_name>/`.
- **BaseExecutionEnvironment** (`environment/`) — code execution sandbox.
  `DockerPytestEnvironment` and `DockerJestEnvironment` run tests in
  containers; `PythonSandboxExecutionEnvironment` runs lightweight checks
  on the host.
- **tool_loop** (`tools/tool_loop.py`) — shared agentic conversation loop
  used by coder, tester, and debugger. LLM calls discovery tools
  (`view_file`, `list_directory`, `search_files`, `search_imports`) before
  submitting final output.

**Pipeline agents (in execution order):**

1. **Planner** (`planner/`) — decomposes project into milestones
2. **Architect** (`architect/`) — decomposes milestone into services, orchestrates full pipeline
3. **Provisioner** (`provisioner/`) — materializes project on disk (skeletons, compose, Dockerfiles)
4. **FusionAuth agent** (`provisioner/fusionauth_agent.py`) — configures auth roles/users
5. **Engineer** (`engineer/`) — analyzes service → issues → dispatches orchestrator
6. **CodingOrchestrator** (`orchestrator/`) — runs Coder→Tester→Debugger loop per issue
7. **UX Designer** (`ux_designer/`) — screenshots frontend views, evaluates design via vision AI, dispatches fixes
8. **HTTPApiTester** / **WebUITester** (`integration/`) — writes + runs integration tests
9. **AgenticDebugger** (`agents/debugger/agentic.py`) — repairs integration failures with discovery tools

**Config system:** `bizniz.yaml` in CWD (or parent dirs) → `BiznizConfig`
Pydantic model (`config/bizniz_config.py`). Routes models by prefix:
`claude-*` → Claude, `gemini-*` → Gemini, else → OpenAI. Key fields:
`architect_model`, `engineer_model`, `planner_model`, `debugger_model`,
`models` (escalation progression), `coder_models`, `tester_models`.

## Testing

```bash
# Run all unit tests (excludes functional tests that call real APIs)
.venv/bin/python -m pytest bizniz/ -q

# Run a specific test file
.venv/bin/python -m pytest bizniz/integration/tests/test_runner.py -q

# Run a single test by name
.venv/bin/python -m pytest bizniz/engineer/tests/test_dependency_graph.py -k "test_cycle" -q

# Run functional tests (call real APIs — needs keys in .env)
.venv/bin/python -m pytest -m functional -q

# Run the standard test suite (as listed in the repo)
.venv/bin/python -m pytest bizniz/integration/tests/ \
  bizniz/architect/tests/ bizniz/workspace/tests/ \
  bizniz/engineer/tests/ -q
```

Tests live alongside their module: `bizniz/<module>/tests/`. Functional
tests (real API calls) are marked `@pytest.mark.functional` and excluded
by default via `pyproject.toml` `addopts = "-m 'not functional'"`.

## Read these next, in order

1. `docs/sessions/2026-05-02_integration_debugger_tuning.md` (latest session — debugger fixes)
2. `docs/sessions/2026-05-01_pipeline_completion.md` (prior session — full pipeline buildout)
3. `docs/changes/2026-05-01_build_vs_evolve_strategy.md` (build-mode now, evolve-mode later)
4. `docs/changes/2026-05-01_pet_groomer_buildout_plan.md` (pet-groomer is the first real customer)
5. `docs/memory/MEMORY.md` — index into the portable memory; each entry points at a specific concern

## Commands you'll need

```bash
# Run the pipeline (default: pet groomer prompt)
cd ~/bizniz && set -a && source .env && set +a \
  && PYTHONPATH=. .venv/bin/python -u examples/auto_architect.py

# Run with no skeleton (apples-to-apples cost experiment)
... examples/auto_architect.py --no-skeleton

# Re-run ONLY integration phase + debugger on an existing project
# (skips engineering — fast iteration on debugger tuning)
PYTHONPATH=. .venv/bin/python -u examples/debug_integration.py \
  ~/bizniz_projects/pet_groomer_v11
# Flags: --backend-only, --frontend-only, --max-iterations 5,
#         --debugger-model gemini-pro

# E2E lifecycle test (property manager)
./tests/e2e/property_manager/run.sh plan    # plan only (~$0.01)
./tests/e2e/property_manager/run.sh m1      # milestone 1 (greenfield)
./tests/e2e/property_manager/run.sh m2      # milestone 2 (evolve)
./tests/e2e/property_manager/run.sh integration  # integration tests
./tests/e2e/property_manager/run.sh up      # stand up for manual review

# Test suite
.venv/bin/python -m pytest bizniz/integration/tests/ \
  bizniz/architect/tests/ bizniz/workspace/tests/ \
  bizniz/engineer/tests/ -q

# Stand a generated app back up after a run (integration phase
# tears down at the end)
docker compose -f ~/bizniz_projects/<slug>/infra/development/docker-compose.yml up -d
```

## Importing memory on a new machine

If you're on a different machine and want auto-memory loading,
copy `docs/memory/*.md` into your local
`~/.claude/projects/<slugified-bizniz-path>/memory/`. The slug is
derived from your local bizniz checkout path (e.g.
`-home-username-bizniz`). Without this, Claude reads memory from
`docs/memory/` only when explicitly pointed at it (which this file
does in step 4 above).

## Key invariants the pipeline depends on

1. **SKELETON.md contract** — every skeleton ships one; engineer reads
   it via `bizniz/workspace/skeleton_conventions.py` and threads it
   into analyze + plan user prompts. Files outside the skeleton's
   declared extension points are dead code in the running container.
2. **Auto-discovery** — FastAPI auto-mounts `app/api/routes/*.py`
   with a `router` attr; React auto-mounts `src/routes/*.tsx` (excluding
   `*.test.tsx`/`*.spec.tsx`) with `default` export of `RouteEntry[]`
   or single `RouteEntry`. Both warn loudly on misshapen modules.
3. **FusionAuth for all auth** — the fastapi skeleton delegates auth
   to FusionAuth. `get_current_user` and `require_roles` validate
   FusionAuth-issued RS256 JWTs. The skeleton never mints tokens or
   hashes passwords. Local User table is a sync copy for FK relationships.
4. **Non-destructive editing** — engineer's prompt has a HARD
   CONSTRAINT against silent rewrites of skeleton-shipped files.
   Prefer adding new files in extension points.
4. **Strict infrastructure** — architect prompt says ONLY add DB/auth/
   cache/queue/etc that the problem statement explicitly mentions or
   genuinely requires. "Real apps need auth" is no longer license.
5. **Integration phase as the source of truth** — unit tests pass
   against mocks; integration tests pass against reality. Customer-
   facing artifacts must pass both.

## What NOT to do

- Don't downgrade `BaseDebugger._ai_client` to use `self._client` —
  the cost-tracker per-call attribution depends on it.
- Don't make WebUITester emit `.ts` files. The Vite frontends set
  `"type": "module"` which breaks Node's ESM strict mode + TS loader.
  `.spec.cjs` with `require()` is the contract.
- Don't add infrastructure auto-discovery in skeletons that
  silently skips on contract violation. Loud warnings only —
  the V9 silent-skip cost us most of a session.
- Don't forget to set `allowedHosts: true` in any new Vite-based
  frontend skeleton. Default Vite blocks docker DNS hostnames.
- Don't reintroduce local JWT minting or password hashing in the
  fastapi skeleton. FusionAuth owns identity. The skeleton's
  `app/core/auth.py` only validates JWTs, never creates them.
- Don't remove the container restart from integration debug `_rerun`
  callbacks. Without it, uvicorn serves stale code and the
  debugger's fixes never take effect (V11 lesson — cost us 3
  wasted iterations and $0.77).
- Don't let the AgenticDebugger's `run_command` or `run_tests`
  become the primary test execution path for integration debugging.
  Tests run in Docker sidecars via the `rerun_tests` callback;
  `run_command` is for grep/find/cat on the host. Use
  `inspect_container exec` for commands that need the container's
  Python/Node environment.
