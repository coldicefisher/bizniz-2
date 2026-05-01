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

## What's in flight (as of 2026-05-01)

- **Just landed**: full integration phase end-to-end — HTTPApiTester,
  WebUITester, AgenticDebugger wired in, .cjs Playwright sidecars,
  contract handoff between layers, skeleton hardening across all 5
  skeletons.
- **V10 result**: pipeline ran fully, integration tests honestly
  detected real bugs in the AI-generated app (backend 9/11 passed,
  frontend 0/6 passed), AgenticDebugger crashed on
  `_ai_client` AttributeError before getting to repair anything.
- **V10 fix shipped**: `BaseDebugger._ai_client` property added
  (commit `ea6aa38`). V11 should run the debugger end-to-end.
- **Pending**: V11 verification run that exercises AgenticDebugger
  against the V10-style failures.

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

1. `docs/sessions/2026-05-01_pipeline_completion.md` (this session's full narrative)
2. `docs/changes/2026-05-01_build_vs_evolve_strategy.md` (build-mode now, evolve-mode later)
3. `docs/changes/2026-05-01_pet_groomer_buildout_plan.md` (pet-groomer is the first real customer)
4. `docs/memory/MEMORY.md` — index into the portable memory; each entry points at a specific concern

## Commands you'll need

```bash
# Run the pipeline (default: pet groomer prompt)
cd ~/bizniz && set -a && source .env && set +a \
  && PYTHONPATH=. .venv/bin/python -u examples/auto_architect.py

# Run with no skeleton (apples-to-apples cost experiment)
... examples/auto_architect.py --no-skeleton

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
