# 2026-05-02 — Integration debugger tuning session

V11 ran the full pipeline end-to-end. The AgenticDebugger engaged
for the first time (the `_ai_client` fix from `ea6aa38` worked).
Three critical bugs in the integration debug loop were found and
fixed. The debugger now repairs integration failures in 1 iteration.

## V11 pipeline results

- **Backend**: PASS 5/5 issues (three-phase: flash-lite framing →
  flash repair → pro escalation for 3 remaining tickets)
- **Frontend**: PASS 4/4 issues (no react skeleton available — fell
  back to generated boilerplate, flash + pro escalation)
- **Integration tests**: backend 9/11 passed (2 real failures),
  frontend never responded on `/`
- **AgenticDebugger**: ran 3 iterations on backend, did not fix
- **Cost**: $3.84 total, $0.77 for debugger (30 calls)
- **Time**: 3218s (~54 min)

## Root causes found

### Bug 1: Container not restarted after code fixes

The debugger wrote correct fixes to `app/api/routes/appointments.py`
on iteration 1, but the running container kept serving stale code.
Uvicorn doesn't auto-reload without `--reload`, and the `_rerun`
callback only re-ran pytest — it didn't restart the container.

**Fix**: `runner.py` now calls `docker compose restart <service>`
and waits for health before re-running tests. Both backend and
frontend `_rerun` callbacks updated.

### Bug 2: Debugger wasted turns running local commands

The debugger tried `python3 -c 'from app.main import app'`,
`pip install`, `ps aux | grep uvicorn`, `pytest` — all on the host
where the app's dependencies aren't installed. It didn't understand
it was editing volume-mounted files, not running the app locally.

**Fix**: System prompt updated with integration testing context.
Explicit instructions: don't run the app locally, don't pip install,
focus on reading code and submitting fixes. The harness handles
container restart and test re-execution.

### Bug 3: Debugger couldn't see server-side errors

The debugger only saw client-side test output (`assert 422 == 200`)
but not the server's traceback explaining WHY. For 400/422 errors
this is the difference between guessing and knowing.

**Fix**: `capture_logs` callback added to `repair_integration_failure`.
Container logs (last 60 lines) are auto-prepended to the error output
on every iteration. Header tells the debugger it's a tail and to use
`inspect_container` for more.

### Enhancement: inspect_container tool

New tool on AgenticDebugger for on-demand container inspection:
- `inspect_container logs` — last 100 lines (default)
- `inspect_container logs 200` — configurable tail
- `inspect_container exec <cmd>` — run commands inside the container
  (pip list, python snippets, etc.)

Replaces the broken pattern of running app-level commands on the host.

## Verification

After the fixes, reintroduced the double-booking bug in the V11
project and ran the standalone harness:

- HTTPApiTester generated fresh tests → 1 failure (double-booking)
- AgenticDebugger iteration 1: diagnosed `missing_implementation`,
  applied fix to `appointments.py`
- Container restarted, tests re-run → ALL PASS
- **1 iteration, $0.05, 75 seconds**

## Artifacts produced

- `examples/debug_integration.py` — standalone harness for re-running
  just the integration phase against an already-built project
- `~/bizniz_projects/pet_groomer_v11/` — V11 project on disk

## Commits

| Hash | Description |
|---|---|
| `bd24e90` | Container restart + prompt tuning + harness |
| `5ad097a` | Auto-tailed server logs in error output |
| `f11ba67` | inspect_container tool + compose_path wiring |

## Known issues surfaced

- **React skeleton missing**: GitHub auth failed on auto-clone, fell
  back to generated boilerplate. Need to manually clone or fix auth.
- **527 source files in frontend repair context**: `node_modules`
  is being included in the workspace file listing, bloating the
  repair prompt. Needs filtering in `_list_relevant_source_files`
  or workspace-level exclusion.
- **Frontend never responded on `/`**: the generated boilerplate
  frontend (no skeleton) doesn't build/serve correctly in the Docker
  container. Vite config / index.html likely misconfigured.
- **Gemini Pro very slow**: some API calls took 2+ minutes, one
  appeared to hang for ~10 minutes before completing. Rate limiting
  or model congestion.

## What V12 should verify

1. Clone the react skeleton manually and re-run to get skeleton-based
   frontend + Playwright tests exercising the debugger
2. Confirm `inspect_container exec` works when the debugger uses it
3. Watch for the node_modules bloat in frontend repair context
