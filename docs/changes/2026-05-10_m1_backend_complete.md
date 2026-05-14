# M1 backend hits 14/14 — failure-context + dispatcher fixes

**Status:** property_manager_v33 M1 backend complete. 13 issues
escalated/passed, 1 deferred (work absorbed), 0 stalled, 0 errored.
Across rounds 4 → 9, total backend spend: **~$1.94**.

## The arc

| Round | Outcome | Cost | Notes |
|---|---|---|---|
| 4 | 11/15 — BE-005-fix1 stalled, BE-005 + BE-007 dep-skipped | $0.97 | First-ever passes after the v25→v32 wall. Triggered by the compose-exec `run_tests` change (e9cc7e9) — tests ran inside the service container with real deps instead of the bare pytest sidecar |
| 5 | 11/15 — same shape | $0.28 | Tested the prompt-only probe-first rule. Telemetry: **0 diagnostic-tool calls** across 27 iter of BE-005-fix1. Cheap-tier models ignore declarative rules |
| 6 | 12/15 — BE-005-fix1 finally escalated | $0.29 | Auto-tail container logs on `TESTS FAILED`. First `tail_logs` of the session. First `hit_endpoint`. First `run_python_in_container`. Wall fell |
| 7 | 12/15 — BE-005 ran but choked on empty-action loop | killed | Dispatcher's `deferred ≠ failed` patch let BE-005 actually run. But flash-lite emitted `action=''` and stall detection didn't catch it (unknown-actions skipped `recent_actions`) |
| 7c | 14/15 — BE-005 ✓, BE-007 partial | $0.31 | Unknown-action signature added to stall detection. fast escalation through tiers when model gets stuck |
| 8 | 14/15 — BE-007 ended `errored` | $0.09 | Last issue: forced-final on flash-top emitted `status='passed'` without green tests, gate rejected, `TerminalActionRejected` bubbled out of `run()`, issue marked non-recoverable |
| 9 | **14/14** | $0.09 | Forced-final TerminalActionRejected now converts to stall → escalates cleanly. BE-007 went green on flash |

## Five compounding fixes

These changed the run from "expensive grinding that doesn't converge"
to "cheap, deterministic, terminates."

### 1. compose-exec for `run_tests` (commit e9cc7e9 — prior session)

Tests run inside the service container, not the pytest sidecar.
The sidecar had only pytest+httpx; v2.5 Coder writes
`from app.main import app` TestClient tests that need sqlalchemy,
fastapi, the actual app — all already in the service container.
This was the wall that v25→v32 hit.

### 2. AUTH_CONTRACT.md extension (commit 7761b8f)

Renderer now appends a deterministic FusionAuth API endpoint
reference: login (no path arg), register (no path arg —
`POST /api/user/registration`), role change (path arg —
`PATCH /api/user/registration/{userId}`), password policy,
JWT validation. Calls out the `[duplicate]registration` 400
pitfall (putting app ID where user ID goes → FA echoes the same
UUID as both userId and registration target).

Code-samples LLM prompt now requires register + role-change
snippets per language.

Per-service workspace copy: AUTH_CONTRACT.md lives both at
project root AND in each service's workspace dir, so any agent
that browses on-disk sees it.

Conditional callouts in Coder + Debugger initial context —
"this is your canonical FA reference, don't guess paths from
memory" — emitted only when the contract file is actually present.
Don't confuse the agent if the file isn't there.

### 3. Auto-tail container logs on test failure (commit 7761b8f)

When `run_tests` returns `TESTS FAILED`, the handler appends:

- target service logs (last 30 lines) — uvicorn access + tracebacks
- auxiliary service logs (auth, db, postgres, redis — 15 lines each) — upstream 4xx/5xx response context

Capped at 6KB total. **This was the actual unlock.** v33 round 5
telemetry: 0 of 21 failed runs prompted a `tail_logs` call despite
the explicit prompt rule. Round 6 (with auto-tail): the Coder
started calling `tail_logs`, `hit_endpoint`, `run_python_in_container`
all by itself. The auto-attached context primed the model to
investigate instead of looping write/test/write.

### 4. Dispatcher: deferred ≠ failed for deps (commit 7761b8f)

Orchestrator was treating any non-`passed`/`escalated` disposition
(including `deferred`) as a dependency failure, blocking downstream
issues. BE-003 ("Implement Queue service") was deferred because its
work was absorbed into BE-002-fix ("Implement Auth and Queue
Services"). BE-005 and BE-007 declared BE-003 as a dep and got
skipped 6 rounds running. Patch: `passed|escalated|deferred` all
satisfy a dependency.

### 5. Stall detection for unknown/empty actions (commits 7761b8f, 07ae439)

Two cliffs:

- **Round 7 (commit 7761b8f):** unknown actions hit `continue` before
  `recent_actions.append`. flash-lite emitted `action=''` 10+ times
  at ~1 min/turn before iter budget exhausted. Now: unknown signatures
  are appended; 3-of-N hits stall threshold → escalate.

- **Round 8 (commit 07ae439):** `TerminalActionRejected` in the
  forced-final path (last-iter, gate-rejection) propagated as
  exception → issue marked `errored`, non-recoverable. Now: convert
  to `ToolLoopAgentStalledError` so the orchestrator escalates to
  the next tier instead of giving up.

## What still bothers me

- **Cheap-tier models don't follow declarative rules.** The
  probe-first prompt rule was right; the auto-tail forcing function
  did the actual work. Future: the prompt-rule investment was wasted
  on flash-lite. Save tier-upgrade money by accepting this and
  building deterministic forcing functions instead of writing more
  rules.

- **The "fix" issue pattern is opaque to the dispatcher.** When
  ServicePlanner repair emits `BE-X-fix1` and absorbs the original
  BE-X's work, the original sits in dep graphs of downstream issues
  but is never run. We solved this by treating `deferred` as
  satisfied, but a real fix is: the planner should rewrite downstream
  `depends_on` to point at the fix instead.

- **No coverage check on what the Coder wrote.** Tests-pass !=
  feature works. Quality engineer / code reviewer phases were
  designed for this; we haven't wired them after v2.5 reset.

## Next

- M1 frontend (never ran in this session — only backend)
- Stand up the stack, manual smoke test the live auth flows
- M2 backend (evolve mode against M1's contracts)
