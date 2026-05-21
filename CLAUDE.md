# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quickstart

This file orients a Claude session in the bizniz repo. Read this
first; it tells you what to load next.

## Current roadmap (2026-05-16)

Locked-in order — work items 1 → 12 in sequence. Full text at
`docs/roadmap.md`. Honor this when prioritizing new work. Item 5
inserted 2026-05-16 after CRM v1 M5 crashed twice on defensive-
handling gaps; items 6-11 renumbered. Items 12-13 appended same
day (immune system + brain). Gemini baseline (was item 11)
deferred 2026-05-16; remaining items renumbered to 11+12.

1. ✅ **Confidence signals load-bearing** — SHIPPED 2026-05-15
   (commit `5de1059`). `QualityEngineer.enrich.confidence` now drives
   the harness: re-enrich at 0.4-0.6, soft gate at <0.4. Meta-pattern
   audit + retrofit for Architect/Planner/Coder/Tester moves to
   item 9.
2. ✅ **Finish UX with Storybook** — SHIPPED 2026-05-17. Storybook
   driver is now wired into ProUXDesigner via v2_build.py
   (`storybook_driver` constructor arg); per-story loop runs
   alongside per-route. React skeleton ships 4 starter primitives
   (Button, FormInput, Modal, Alert) with `.stories.tsx`, plus the
   existing Toast — so Storybook discovery has something to find
   on every fresh project. Sub-tickets shipped along the way:
   design-system-lock (`fd72c94`), orphan-shot-fallback (today).
   Angular skeleton variants deferred (React loop proves out first).
3. ✅ **Add version control** — SHIPPED 2026-05-15 (commit
   `8c65435`). `ProjectGit` writes `m0` tag after provisioner +
   `m<N>-done` tags after each milestone. Phase-level commits not
   wired yet (milestone granularity is sufficient for refactor
   revert).
4. ✅ **Granular issue decomposition** — v1 SHIPPED 2026-05-15
   (commits `ae3883a`, `9f8a652`, `0e6fe0d`). `Decomposer` agent
   breaks each issue into ordered `UnitOfWork`. **Always on by
   default**; opt-out via `v2_build --no-decompose` (escape hatch
   for A/B comparison runs). Live validation still pending.
5. ✅ **Agent error-path audit** — SHIPPED 2026-05-16, extension
   sweep 2026-05-17. Every agent's `raise` paths classified
   (fatal / lenient / transient / auto-fill) in
   `docs/agent_error_audit.md`. 7 lenient-path fixes shipped under
   the original sweep; 2026-05-17 follow-up sweep verified 12
   additional raise sites are correctly absorbed by their callers
   (Engineer via tool_loop, Provisioner AI fallback/recovery via
   `_build_ai_fallback_template`/`_try_ai_recovery_for_build`,
   WebUITester via `integration_phase`, refactorer tokenizers are
   defensive/unreachable). No outstanding fixes.
6. **Refactorer agent** — dedupe + move shared business logic to
   `shared/<lang>/` core libs. Consumes item 4's atomic issues.
7. **Tests / debugging after refactoring** — catch refactor-induced
   regressions automatically; also extend smoke-recovery (already
   shipped one-shot in `29e5ea9`) to multi-tier escalation here.
8. **Human documentation system** — agents write semantic docs per
   service (README, API reference, architecture, how-to-extend).
9. **Detailed diagnostic + performance logging** — structured
   per-call timing/tokens/cache-hits → `performance.json`. Pipes
   the confidence-signal retrofit (Architect, Planner, Coder,
   Tester self-rating) onto the same instrumentation. Phase 1
   shipped 2026-05-16 (`bizniz/perf_log/` — regex log analyzer +
   markdown/JSON formatters + comparison mode for A/B testing).
   Phase 2 (structured emit at the source) lands with the rest of
   item 9.
10. **Performance test on Claude** — 3-5 reference projects, baseline
    established with $0 marginal cost.
11. **Immune system** — wire 6+7+8 into a canonical
    `Refactor → Full test suite → Document` cycle that gates every
    milestone DONE. Evolve mode reorders: `Write tests → Refactor →
    Document → Work tickets`. Each step is a hard gate.
12. **Brain** — bounded self-evolving A/B testing against a
    reference problem. Picks a knob (prompt version, confidence
    threshold, iteration cap, model tier), runs the reference,
    compares via `perf_log`, promotes if better, stops after N
    iterations without improvement. Operator reviews + merges
    final change. Open-ended self-modification is explicitly out
    of scope.

**Deferred 2026-05-16**: Gemini baseline (was item 11). Claude
pipeline is the primary investment ($0 marginal on Max plan);
Gemini benchmarking can wait until items 10-12 ship.

Deferred (do NOT pull forward unless explicitly asked):
- Angular skeleton Storybook variants (until item 2 proves React
  loop end-to-end).
- Production-mode Dockerfile variants (until dev-mode loop is
  stable).

## Session state — 2026-05-20 (mid-flight)

**Live build in progress: `recipe_v4_v16` (--use-v5).** Started
14:53:39. As of 16:55: M1 in repair iter 3, ResolutionChecker
running. Auto-kill watcher armed (background bash, task id
`bxrygrnq9`) — fires `pkill -f v2_build.*recipe_v4_v16` the
moment `MilestoneLoop: M2` appears in the log. User wants M1
only; M2+ deferred to next week.

Log: `/tmp/bizniz_runs/recipe_v4_v16.log`.

### What shipped tonight (5 commits, all on main)

1. `7fceb71` v5 hotfix: edit-mode supports new_files
2. `f7742de` container_rebuild image-only path when container down
3. `4a97770` container_rebuild health check best-effort (warn, not fail)
4. `f28a1e2` FIX-1: flatten layers at IMPLEMENT — concurrent services
5. `0ef4b27` FIX-2: ResolutionChecker balanced verdict (drop "prefer still_present")
6. `c153ebe` v5 hotfix: ResolutionChecker now sees workspace files
   (the night's main fix — `_collect_files_for_check` now uses a
   `discover_workspace_files()` closure that walks the workspace
   and returns code/test paths. Pre-fix: checker got ZERO files
   for QE coverage findings → judged everything `still_present`
   forever. Post-fix: checker gets 60 files.)
7. `5b4a511` v5 hotfix bundle (three fixes, one commit):
   - **Fix A**: `_materialize_seed` protects manifest files
     (`_PROTECTED_MANIFEST_FILES` set: requirements.txt,
     package.json, Dockerfile, pyproject.toml, etc.) so
     ServicePlanner can't stomp the skeleton's pytest/asyncpg
     by emitting a hallucinated "summary" requirements.txt.
   - **Fix B**: `system_prompt_with_scaffold.py` explicitly
     forbids seeding manifests + tells the planner that runtime
     dep changes flow through Coder's `requested_deps`.
   - **Fix C**: `PerIssueValidator` takes an `on_deps_changed`
     callback; `V4MilestoneCodeDispatcher` provides a closure
     wrapping `_apply_requested_deps` + `maybe_rebuild`. Fires
     after each fix iter that returned `requested_deps`. Pre-fix:
     agent could request pytest mid-loop, request was dropped
     on the floor, validator stalled forever on the same import
     finding.

Tests: 24 + 4 + 4 + 4 = 36 new tests, 0 regressions across
per_issue_validator + driver + resolution_checker + service_planner.

### v16 run evidence (the data the night was about)

**IMPLEMENT phase (the big win):**
- Backend: 10/10 issues CLEAN, 0 debug iters, 191s wall
- Frontend: 8/8 issues CLEAN, 0 debug iters, 239s wall
- Total IMPLEMENT: ~4 min vs typical 30-60 min (~8-15×)
- Live `requirements.txt` preserved as full skeleton content
  (pytest, pytest-asyncio, asyncpg, alembic, all 16 lines).
  Fix A validated.
- No `SKIPPED seeding` warnings → planner didn't try. Fix B
  validated.
- Fix C exercised live at 15:23:33 on BA-fix1-1: agent
  requested `respx`, `_apply_requested_deps` appended,
  container rebuilt, validator clean after 1 debug iter. ✅

**Review/repair convergence (tonight's main fix):**
```
Iter 1: 48 findings (47 blockers) frozen as CanonicalReport
        ResolutionChecker: 48 findings checked against 60 files ✅
        (would have been 0 files pre-fix → 70/70 still_present)
Iter 2: 6 findings remain (87% closed in one cycle)
        CR side: all resolved (nothing to check)
        QE side: 6 still_present → PerMilestoneDebugger fires
        PerMilestoneDebugger: clean in 11min, touched 4 files
        Re-check: still 6 still_present (checker disagrees w/ debugger)
Iter 3: dispatched 4 BE fix-issues + 4 FE fix-issues
        ALL agents returning empty edits + new_files → BROKEN
```

### Two NEW bugs surfaced in v16 (next-week tickets)

1. **"Empty edits + new_files" no-op refusal.** When the
   fix-pass agent decides "the seeded scaffold already
   addresses this, nothing to change," it returns empty
   `edits` + empty `new_files`. Validator's salvage refuses
   the no-op → issue marked BROKEN. Hit FR-fix2-4, FR-fix2-5,
   BA-fix3-1, BA-fix3-2, BA-fix3-3, BA-fix3-4, FR-fix3-3
   (and counting). Fix: salvage should accept "no-changes-
   needed with notes" as a CLEAN no-op signal, not BROKEN.
   File: `bizniz/coder_tester/agent.py` (search "empty edits +
   new_files. Refusing to ship a no-op result.").

2. **ResolutionChecker too conservative on some QE findings.**
   Post-Fix-2 the prompt is balanced, but the checker still
   says `still_present` for findings 5 different agent votes
   say are addressed (PerMilestoneDebugger + 4 fix-issue
   Coders). Weight of evidence says checker is wrong on these.
   Could be: (a) prompt needs more guidance on scenario
   findings vs file-anchored, (b) needs to see actual test
   run output not just code, (c) the QE finding text itself
   is ambiguous. Worth investigating before next live build.

### Three deterministic-context levers (user-requested 2026-05-20)

User asked: "what can we load deterministically as context, cross-
language + cross-platform, simple, universal?" Top three picks:

1. **AST-extracted symbol tables** (tree-sitter, every language).
   Pre-digest "what's exported from each file" instead of LLM
   re-reading.
2. **Workspace diff since last call** (`git diff`). Tells LLM
   "you saw old state; here's what changed" instead of paying
   to re-read.
3. **Structured test failure extraction** (parse pytest/jest
   output into (file, line, assertion, expected, actual)
   tuples). Currently dumps raw output.

Cheap first experiment: add `outcome_signal` field to the cost
ledger entries; pick CoderTesterAgent in IMPLEMENT; A/B test
WorkspaceDiff context vs none. Measure `tokens / finding_resolved`
across 3 builds with vs 3 without.

### Open backlog (from prior session, still valid)

- Skeleton Tailwind wiring (UX fixes don't render).
- Stage 2b refactorer iteration if quality varies.

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

## What's in flight (as of 2026-05-13)

### Post-milestone phases ✅ shipped end-to-end

UX_REVIEW + REFACTOR fire after INTEGRATION_WEB on every milestone,
before DONE. Recipe_box ran all four milestones front-to-back
including both phases on the final milestone.

- **`bizniz/driver/ux_phase.py`** — runs UXDesigner per frontend
  service. Self-skips when there's no frontend or no factory. UX
  failures are recorded but don't gate the milestone (informational).
- **`bizniz/ux_designer/claude_ux_designer.py`** — vision eval via
  `claude --print --add-dir <screenshots_dir>` with Read tool, not
  inline Gemini images. $0 marginal on Max plan. Same parent class
  handles screenshot capture (Playwright sidecar) and fix dispatch
  (ClaudeCliCoder). v2_build selects ClaudeUXDesigner when the
  `claude` binary is on PATH; falls back to legacy Gemini path
  otherwise.
- **`bizniz/driver/refactor_phase.py` + `bizniz/refactorer/`** —
  fires when the Planner flagged `milestone.refactor_after=True` OR
  the milestone is the final one (always treated as a refactor
  boundary). Single Claude Code CLI session rooted at the project
  so it sees every service workspace; scans for cross-service
  duplication, extracts to `shared/<lang>/`, updates consumer
  imports + dependency manifests, runs tests, reverts on failure.
  Output: structured `RefactorerResult` (status, extractions list,
  skipped candidates with reasons, notes).
- **`Milestone.refactor_after: bool`** — Planner-emitted hint;
  prompt teaches when to set True (CRUD domain closing, admin
  mirrors user surface, second API consumer).

### Recipe_box 4-milestone end-to-end ✅ first complete run

Generated 2026-05-12, completed 2026-05-13. M1 auth+dashboard,
M2 create/list recipes, M3 view/edit/delete, M4 admin views — all
DONE with real working CRUD verified by curl (login → POST → GET →
PUT → DELETE all green). Final REFACTOR pass extracted the recipe
validation error formatter (duplicated across POST/PUT routes),
tests passed.

UX_REVIEW on M4 with ClaudeUXDesigner: captured 12 screenshots,
117s vision eval, found Tailwind not actually wired into build
(real diagnostic Gemini's 4s eval missed). 27 fix attempts across
2 iterations — followup needed at the skeleton level since
Tailwind config is missing.

### Resilience fixes that landed this run

- **ClaudeCliClient 429 backoff** — Anthropic transient rate limits
  retry with 10s/30s/60s schedule before failing. Distinct from Max
  usage cap.
- **ClaudeCliClient `--disallowedTools`** — basic single-call client
  was returning narrative summaries instead of code on WebUITester
  prompts because Claude treated "write the test file" as a Write-
  tool task. Explicit disallow forces pure text output.
- **Claude CLI subprocess timeouts → 1800s** across the board.
  600s was tripping on tool-heavy debugging.
- **Web debugger wired into `run_web`** — symmetric with run_api.
  Was claimed in the docstring but never actually plumbed.
- **SmokePhase + integration runners use `docker compose port`** for
  host-side URLs — project-collision-proof. Architecture.port stays
  as the container port (correct for docker-internal sidecar URLs).
- **`/health` readiness gate after debugger rebuilds** — pytest no
  longer fires before uvicorn is accepting connections.
- **Coder in-container dep install** (#72) — when ClaudeCliCoder
  edits requirements.txt or package.json, it hashes the manifest
  before/after and runs `docker compose exec <svc> pip install -r
  ...` / `npm install` inside the running container before
  returning. Cleaner than image rebuild; matches what Coder was
  doing manually.

### Open backlog

- **Skeleton Tailwind wiring** — ClaudeUXDesigner found Tailwind
  classes are written but the CSS isn't being processed. React
  skeleton needs Tailwind installed + config wired so UX fixes
  actually render.
- **Stage 2b iteration** — Refactorer is minimum-viable (single
  LLM session does everything). If quality varies on bigger
  projects, add a deterministic candidate detector + per-candidate
  dispatch with escalation.
- **`bizniz.yaml`** — accidentally reverted to Gemini defaults
  mid-session; user may want to restore their claude-cli config
  for non-coder roles.

## Previous: 2026-05-12 — Claude pivot complete

### Pluggable LLM backend — Architecture C ✅ complete

The pipeline now runs on either Gemini API or Claude Code CLI
(subprocess) interchangeably. Same orchestrator, same agents, same
workspace artifacts — config selects per-agent per-service.

- **`bizniz/clients/claude_cli/`** — `ClaudeCliClient(BaseAIClient)`
  shells out to `claude --print --output-format=json`. Free on Max
  plan, metered API rates on Pro/Free. Routes via `claude-cli` model
  prefix. Single-call agents (Planner, Architect, ServicePlanner,
  AuthPlanner, QE, CR, code_examples) just work.
- **`bizniz/coder/claude_cli_coder.py`** — `ClaudeCliCoder` for the
  tool-loop. Same constructor surface as `Coder`; the dispatcher
  swap is config-only (`coder_factory` checks the model name).
  Claude uses native Edit/Write/Read/Bash/Glob/Grep tools with
  `--permission-mode=bypassPermissions` against the service workspace.
  Final output: a CoderResult JSON we parse.
- **`bizniz/mcp_server/`** — MCP server exposing five Bizniz tools to
  Claude on demand: `get_prior_issues`, `get_issue_test_output`,
  `validate_python_imports`, `read_audit_findings`,
  `read_auth_contract`. ClaudeCliCoder writes a temp mcp-config.json
  per-issue pointing at the server (launched as `python -m
  bizniz.mcp_server.server` with `BIZNIZ_PROJECT_ROOT` +
  `BIZNIZ_JOB_ID` env vars). Live-verified: Claude called
  `mcp__bizniz__get_prior_issues` against bookshelf_claude's DB and
  returned the right 8 issue IDs.

### Full MilestoneLoop — all phases wired

`ENRICH → IMPLEMENT → SMOKE → REVIEW_INITIAL → REPAIR_ITER_{0,1,2} →
REVIEW_FINAL → INTEGRATION_API → INTEGRATION_WORKER → INTEGRATION_WEB
→ DONE`. Each step has a hard gate via `GatePolicy`.

- **`bizniz/driver/smoke_phase.py`** — new SubPhase.SMOKE. Pure curl
  against the live compose stack: backend `/health` + public-flow
  `/api/login` (no API key — same path the SPA uses) + GET probes on
  every registered OpenAPI route. Any 5xx fails the gate. Catches
  the "tests pass but app 500s" class.
- **QualityEngineer + CodeReviewer** — wired in MilestoneLoop's
  `_phase_review` after IMPLEMENT. QE checks coverage by capability
  (returns CoverageReport with missing scenarios); CR checks code
  quality (flagged_symbols, ungated_auth, missing_error_handling).
  Drives REPAIR_ITER iterations.
- **IntegrationPhase** — `run_api`, `run_worker`, `run_web` for
  HTTPApiTester + WorkerTester + WebUITester + AgenticDebugger.
  Fully end-to-end: capture OpenAPI → write tests → run pytest
  sidecar → on fail dispatch debugger → iterate.

### Coder hardening

- **`bizniz/coder/symbol_validator.py`** — AST walker catches
  hallucinated imports AND attribute access on Pydantic/dataclass
  classes (`settings.foo_bar` when only `foo_baz` exists).
  Caught two real v33 bugs the pipeline had shipped.
- **Don't-swallow-exceptions** prompt rule — Coder forbidden from
  generic `except Exception: raise HTTPException(500, "Internal
  server error")` patterns. v33 wasted an hour diagnosing a swallowed
  AttributeError.
- **Probe-first rule** in Coder + AgenticDebugger prompts (cheap
  tiers ignore it; auto-tail-on-failure in `lib/tools/test_runner.py`
  is the deterministic forcing function — it auto-appends container
  logs of target + auxiliary services to every TESTS FAILED output).
- **Forced-final TerminalActionRejected → stall** (not errored).
  Issue gets cleanly escalated to the next tier instead of marked
  non-recoverable.
- **Unknown/empty action stall detection** — `tool_loop_agent` counts
  empty `action: ""` in `recent_actions` so 3-of-5 fires the stall
  signal. Without this, flash-lite's empty-action loops burned the
  iter budget.

### AuthOperator now matches user-facing reality

- **`requireAuthentication=false`** on app create + reconcile PATCH
  on existing apps. SPA frontend can call `/api/login` without an
  API key (FA default `true` blocked this).
- **`_smoke_login` uses public flow** (no API key) — the manifest's
  `login_verified=true` field is now grounded in the same path the
  frontend takes.
- **Contract extension**: deterministic FusionAuth API endpoint
  reference (login, register, role-change, password policy, JWT
  validation). Calls out the `[duplicate]registration` 400 pitfall
  (putting app ID where user ID goes). Per-service workspace copies.

### Validation runs

- **bookshelf greenfield on Gemini** (2026-05-11): 6 issues +
  3 fixes, 2 repair iterations, halted at milestone_unapproved gate
  with 4 critical CR findings. ~50min, $1.49.
- **bookshelf_claude greenfield on Claude** (2026-05-11): 8 issues,
  **0 escalations, 0 stalls, 0 repair iterations**. QE approved
  5/5 capabilities first try. CR approved with 2 findings, 0 critical.
  ~40min, **$0 marginal** (Max plan).
- Same problem, same pipeline. The Gemini-flash quality ceiling vs
  Claude is dramatic. See
  `docs/changes/2026-05-12_full_pipeline_and_claude_pivot.md`.

### Pending (rolled into open backlog above)

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

**Canonical entry point: `examples/v2_build.py`.** The older
`examples/auto_architect.py` and friends predate the v2.5 refactor
(2026-05-06) and broke when modules moved; they live in
`examples/_deprecated/` for git archaeology. The smoke test at
`tests/test_examples_smoke.py` keeps the current set honest.

```bash
# Run the pipeline on a fresh project
cd ~/bizniz && set -a && source .env && set +a \
  && PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
       --project <slug> --auto "$(cat path/to/prompt.txt)"

# Pre-canned prompts under examples/prompts/
cd ~/bizniz && set -a && source .env && set +a \
  && PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
       --project crm_v1 --auto "$(cat examples/prompts/crm.txt)"

# Plan only (cheap dry-run)
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project <slug> --plan-only "<problem statement>"

# Run a specific milestone (1-indexed, inclusive)
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project <slug> --milestone 2 --auto "<problem>"

# Resume from the most recent run (no problem statement needed)
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project <slug> --resume --auto

# Run ONE phase only
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project <slug> --milestone N --phase integration_api

# Re-run ONLY integration phase + debugger on an existing project
# (skips engineering — fast iteration on debugger tuning)
PYTHONPATH=. .venv/bin/python -u examples/debug_integration.py \
  ~/bizniz_projects/pet_groomer_v11
# Flags: --backend-only, --frontend-only, --max-iterations 5,
#         --debugger-model gemini-pro

# Re-run ONLY the UX phase on an existing project
PYTHONPATH=. .venv/bin/python -u examples/debug_ux.py \
  ~/bizniz_projects/<slug> --debug

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

### Claude CLI rate-limit handling

Two env vars surface controls for the Max-plan 5-hour rolling usage
window so long builds (5+ milestones) survive a window roll:

- `BIZNIZ_CLAUDE_USAGE_CAP_MAX_WAIT_S` — max seconds the client
  will sleep when it parses a `resets HH:MMam` string from a 429
  body. Default 6h (above the typical 5h window). Set to 0 to
  effectively disable wait-on-reset and force a hard fail.
- `BIZNIZ_CLAUDE_FALLBACK_MODEL` — if set, every Claude CLI
  invocation gets `--fallback-model <name>` appended. When the
  primary is overloaded, CLI auto-switches. Example:
  `BIZNIZ_CLAUDE_FALLBACK_MODEL=claude-haiku-4-5`. Trades quality
  for "keep moving during rate-limit windows."

Both are opt-in; defaults preserve existing behavior. Transient
(non-usage-cap) 429s still use the 10/30/60s backoff and hard-fail
after 4 attempts.

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
