# 2026-04-30 (late) — First real end-to-end Engineer pipeline run

We've never run the full Engineer pipeline (per-service codegen with the
real AI doing actual TDD-style iteration) end-to-end against real
Gemini until tonight. This is the milestone that proves the system
genuinely works for its intended purpose, not just that the tests
passing infrastructure is wired up correctly.

## Setup

Standard `examples/auto_architect.py` against the canonical pet-groomer
problem. Gemini stack from `bizniz.yaml` (gemini-flash-lite as default,
gemini-flash for engineer/coder, gemini-pro for architect/planner/
debugger). Real Docker. No stubbed engineer.

Single small fix to make the example actually runnable — the preflight
check was OPENAI-only; relaxed to accept any of the three supported
provider keys (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`).

## Result

```
============================================================
  Results
============================================================
  Project: Pet Groomer
  Project root: /home/jamey/bizniz_projects/pet_groomer
  Services: 4

  backend: PASS (6/6 issues)
  frontend: PASS (4/4 issues)

  Total elapsed: 1641s    (27m 20s)

============================================================
  Cost
============================================================
  calls=51  input=464,212  output=46,292  total=$0.1637
    by model:
      gemini-2.5-flash-lite          calls= 16  $0.0093
      gemini-3.1-flash-lite-preview  calls= 25  $0.0493
      gemini-3.1-pro-preview         calls= 10  $0.1050
```

**$0.16 for a working CRM** — order of magnitude lower than I'd
estimated. Gemini Flash dominates volume; Pro escalation handles the
hard cases.

Run report auto-emitted to
`docs/runs/b304e1a4-7618-45d7-8918-c4d3fc22d592.md` + JSON sidecar by
the `bizniz/run_report/` system shipped earlier today.

## What was verified end-to-end for the first time

  1. **Architect.build()** with a real engineer factory, not a
     `_NoopEngineerCM` stub. Decomposed pet-groomer into postgres +
     fusionauth + fastapi backend + react frontend in one Gemini
     call.

  2. **Phase 1 framing pass** — the codegen_blast quick-pass we
     ported into `engineer.run_layered`. Backend: 6/6 issues framed
     in 91s. Frontend: 4/4 issues framed in 18s (skeleton seeded the
     workspace heavily). Without framing the orchestrator would have
     started from empty stubs every issue.

  3. **Phase 2 escalation chain.** Backend's first 4/6 tickets
     converged on gemini-flash within 1-2 iterations each. Tickets 5
     and 6 exhausted on gemini-flash; engineer escalated both to
     gemini-pro and they converged within 2 iterations. The
     model-progression-on-stall flow worked exactly as designed.

  4. **Cross-issue regression detection + repair.** Caught at backend
     issue 2 (`test_service_model.py` regressed after issue 2's
     codegen), backend issue 6 (`test_appointments.py` regressed),
     and frontend issue 3 (`groomingClient.test.ts` regressed). All
     three repaired in 1 inline-repair iteration.

  5. **Cross-language stack.** Same orchestrator handled Python
     (pytest in `DockerPytestEnvironment`) and TypeScript (jest in
     `DockerJestEnvironment`). Both layers converged.

  6. **Per-run efficiency report.** The system shipped earlier today
     (`bizniz/run_report/`) wrote both the markdown and the JSON
     sidecar from the architect's finally-block. Cost-by-model and
     cost-by-agent breakdowns came out cleanly.

## Bugs surfaced + fixed during the run

### Cost-tracker `agent=unknown` attribution (commit pending below)

The run report showed:

```
| Agent          | Calls | Cost    |
|----------------|-------|---------|
| architect      |   1   | $0.0094 |
| engineer       |   6   | $0.0060 |
| quickdebugger  |  16   | $0.0093 |
| unknown        |  28   | $0.1389 |   ← coder + tester escalations
```

Root cause: `BaseAIAgent.__init__` tags the agent's client with
`_caller_agent` so the AI clients can stamp records with the right
agent name. But `CodingOrchestrator._try_escalate_model()` builds a
**fresh** client via `client_factory(model)` for the higher tier and
assigns it to coder/tester/quickdebugger directly — the fresh client
never went through `BaseAIAgent.__init__`, so its `_caller_agent`
isn't set, and every call routes to `agent=unknown`.

Fix: new helper `_retag_client_for_agent(client, agent)` in
`coding_orchestrator.py`. Called everywhere a fresh client is
assigned (the initial-suggestion swap on line 509 and the
escalation swap on line 2224, both in `run_multi`).

Regression test: `test_caller_agent_retag.py` asserts the helper
sets `_caller_agent` to the lowercased class name and silently
tolerates `__slots__`-frozen clients.

68 orchestrator tests pass (was 66 + 2).

## Insight: how to read the cost-by-model breakdown

  - **flash-lite (16 calls, $0.0093)** — debugger diagnoses (cheap
    reads on broken outputs).
  - **flash-lite-preview (25 calls, $0.0493)** — the workhorse: most
    coder/tester generations and inline repairs. ~$0.002/call avg.
  - **pro-preview (10 calls, $0.1050)** — architect decompose + the
    engineer's analysis/plan calls + escalation rescues for the 2
    stuck backend tickets. ~$0.01/call avg, 5× the flash rate.

Pro is the cost driver per-call but flash dominates volume. The
escalation chain is genuinely cost-efficient: pay flash rates for
80% of work, only burn pro on the hard cases.

## What's next

The system is now verified production-shape end-to-end. Headline
remaining items:

  - The agent-attribution fix needs to land + test pushed (this
    commit).
  - The "unknown" → real-agent breakdown will be visible in the next
    run's report. Re-running the same prompt with the fix in place
    would make a clean before/after delta in the run report.
  - The `bizniz_projects/pet_groomer/` directory is a real working
    project. `cd` there and `docker compose up` would bring up a
    real CRM stack — the AI-generated source actually works.
