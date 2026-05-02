---
name: Integration debugger tuning (V11)
description: Three bugs fixed in integration debug loop — container restart, prompt context, server logs. Debugger now fixes failures in 1 iteration.
type: project
---

V11 ran the full pipeline. AgenticDebugger engaged for the first time (ea6aa38 fix worked) but failed to fix 2 backend integration test failures across 3 iterations.

**Why:** Three bugs in the debug loop:
1. Container not restarted after code fixes — uvicorn served stale code
2. Debugger wasted turns running pip/pytest on host where deps aren't installed
3. Debugger only saw client-side assertions, not server-side tracebacks

**How to apply:** All three fixed. Container restart in _rerun callbacks, prompt tuned for Docker context, server logs auto-tailed + inspect_container tool added. Verified: 1 iteration, $0.05, 75s. The debug_integration.py harness enables fast re-runs without re-engineering.
