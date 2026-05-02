# Bizniz — Claude Quickstart

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

## What's in flight (as of 2026-05-02)

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
- **Pending**: clone react skeleton manually, run V12 with skeleton
  frontend to exercise WebUITester + Playwright debugger path.

## Where things live

| What | Where |
|---|---|
| This repo (orchestration) | `~/bizniz/` |
| Generated apps | `~/bizniz_projects/<slug>/` |
| Per-run reports | `<project>/docs/runs/<job_id>.md` (and .json) |
| Skeleton repos (5) | `~/bizniz-skeleton-{fastapi,react,angular,teams,saas}/` |
| Auto-memory (this machine) | `~/.claude/projects/-home-jamey-bizniz/memory/` |
| Portable memory copy (this repo) | `docs/memory/` |
| Session narratives | `docs/changes/<date>_<topic>.md` |
| Strategy / plans | `docs/changes/2026-05-01_*.md` (pet-groomer, build-vs-evolve) |

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
3. **Non-destructive editing** — engineer's prompt has a HARD
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
