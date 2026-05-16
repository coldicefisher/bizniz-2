# `project.db` readonly-database crash after long-running milestones

Filed 2026-05-15 during M5 of crm_v1. **Reproducible**, halted M5
twice at the same call site.

## Symptom

```
sqlite3.OperationalError: attempt to write a readonly database
  File "/.../bizniz/project/project_db.py", line 935, in mark_issue_finished
```

Triggered when ``IssueStateStore.mark_finished`` runs an UPDATE on
``coder_issues``. The Coder subprocess for the unit completed
successfully; only the state row update fails.

## Repro pattern

Both crashes happened ~hours into a build:
- Crash 1: M5 implement, BE-005-U1 finished at 21:57:31.
- Crash 2: M5 repair iter 1, ServicePlanner emitted fix issues at 23:21:54.

Both followed substantial prior activity: decomposition (~10
issues × ~25s), 20+ Coder subprocesses, smoke recovery (one round),
QE review, and a partial repair pass. Common element: many writes
to ``project.db`` from a single long-lived ``ProjectDB._conn``,
with intervening subprocess calls (Coder, MCP server, SmokeRecovery,
git operations).

After the crash, an external probe (separate Python process) opens
the same db, writes successfully, and exits — confirming **the
file is writable**. The dying process's specific ``sqlite3.Connection``
believes it's readonly.

## What rules out the obvious

- ✅ File perms: ``-rw-r--r-- jamey jamey`` — owner can write.
- ✅ Dir perms: ``drwxrwxrwx`` — anyone can write.
- ✅ Disk space: 663G free.
- ✅ Owner: jamey (same as the running process).
- ✅ ``_ensure_writable`` chmod 0o666 ran at startup.
- ✅ Process is the only v2_build instance (no zombies).

## Likely candidates

1. **MCP server holding a lock** — ClaudeCliCoder spawns
   ``mcp_server`` subprocesses that open ``project.db`` with default
   r/w mode for SELECT queries (``server.py:104``, ``:153``). If a
   stale MCP connection persists across milestones, SQLite could
   degrade. But the error would be ``locked``, not ``readonly``.
2. **Git operations** — ``ProjectGit.commit_all`` calls ``git add -A``
   which reads tracked binaries including ``project.db``. Git
   doesn't chmod, but might briefly take a shared filesystem
   advisory lock on Linux some platforms route into SQLite's
   readonly detection.
3. **Cost ledger** — ``bizniz/cost/ledger.py:187`` opens ``project.db``
   from a different code path. Multiple connection paths to the
   same db. SQLite's connection-pool semantics under WAL vs
   rollback-journal can produce stale-readonly handles.
4. **Connection state transition** — ``ProjectDB._conn`` is a
   single long-lived handle. After hours of operation across
   thousands of writes and many subprocess boundaries, SQLite may
   have transitioned the handle into a degraded state.

Without instrumentation, can't distinguish (1)-(4).

## Workarounds (in priority order)

1. **Cheap retry-with-reconnect** in ``project_db.py``: catch
   ``OperationalError("readonly")`` on UPDATE/INSERT, close+reopen
   the connection, retry once. Probably fixes 90% of cases at
   trivial cost.
2. **Explicit chmod 666 on entry to every write path** in
   ``project_db.py``. Defensive against external chmod.
3. **Open connection per-write** (heavyweight but eliminates
   stale-handle class entirely).
4. **Audit and document all sqlite3.connect() paths** to
   ``project.db``: ProjectDB, cost.ledger, mcp_server. Ensure they
   coexist correctly under WAL.

## Validation we DID get from this M5 run

Don't lose: the run exercised + validated several recent landings
before crashing:

- ✅ Decomposer fired live on 17 issues → 29 units
- ✅ Per-unit Coder timings improved (p95 572s → 279s, max 1072s → 340s)
- ✅ SmokeRecovery fired + recovered successfully (first live test)
- ✅ ProjectGit initialized + ``m0`` tag committed
- ✅ Resume correctly reuses prior unit completions via issue_store

## Acceptance

- Workaround (1) lands: retry-with-reconnect on readonly UPDATEs.
- A repro test (synthetic — long-running ProjectDB with many
  writes interleaved with subprocess calls) demonstrably crashes
  WITHOUT the fix and passes WITH it.
- crm_v1 M5 reruns from the current state and completes without
  the same crash.

## Related files

- ``bizniz/project/project_db.py:35`` — connection construction
- ``bizniz/project/project_db.py:935`` — failing UPDATE
- ``bizniz/state/issue_store.py:144`` — caller of mark_finished
- ``bizniz/mcp_server/server.py:104, 153`` — second connection path
- ``bizniz/cost/ledger.py:187`` — third connection path
