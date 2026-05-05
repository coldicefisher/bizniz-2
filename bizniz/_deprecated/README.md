# `_deprecated/` — v1 LLM-orchestration code (do not import in v2)

Everything here was the v1 multi-agent pipeline. It is **kept as
reference material** for the v2 rewrite — not for active use. Do not
add new imports against this directory. Existing imports in v1
example scripts (`examples/auto_architect.py`, `examples/milestone_build.py`,
etc.) will break until those scripts are updated to the v2
`ServiceImplementer` shape.

## What's here and why

| Subdir | Replaced by (v2) | Why moved |
|---|---|---|
| `coder/` | `ServiceImplementer` | Single tool-using agent writes code+tests+runs them itself |
| `tester/` | `ServiceImplementer` | Was a separate agent that overfitted to the just-written code |
| `quick_debugger.py` | `AgenticDebugger` (kept, w/ live introspection tools) | One smart debugger w/ tools handles inline + agentic |
| `issue_enrichment/` | `ServiceImplementer` (infers from context) | Was scaffolding for cheap models that can't infer structure |
| `orchestrator/` | `ServiceImplementer` self-loop | Three-phase strategy + ModelProgression + governance loop is gone |
| `engineer/` | `Architect` + `ServiceImplementer` | analyze/plan/refine/scaffold collapse into Architect (planning) and ServiceImplementer (per-service work) |

## What v1 got right (keep these patterns in v2)

- **Phase artifacts as files** (plan.json, AUTH_CONTRACT.md, OpenAPI captures)
  — deterministic hand-off between phases via the filesystem, not in-memory state.
- **Layer-transition contract capture** — backend OpenAPI captured before
  frontend dispatch so frontend Implementer doesn't have to guess.
- **Sticky repair log** — every debug attempt reads prior diagnoses.
- **Service-type registry** — adapters per stack (FastAPI, React, Angular).
- **Skeleton conventions** — every skeleton ships a SKELETON.md contract.

## What v1 got wrong (fix in v2)

- **Too many narrow agents.** Coder/Tester/QuickDebugger/IssueEnrichment
  all reinventing tool-use loops. One smart agent with the right tools wins.
- **JSON action schema marshaling.** Every agent's prompt was a
  schema-bound action shape. Modern tool-using LLMs don't need that
  layer; they call tools natively.
- **Coder/Tester separation produced overfitting.** Tester saw
  the code, wrote tests against the buggy code, both passed green;
  integration tests caught the real bugs. Should have been one
  agent or a spec-anchored TestReviewer (sees tests + spec, NOT code).
- **Domain-specific examples in prompts** (property/landlord/tenant/etc.)
  biased the AI toward the project we'd been iterating on.

See `docs/changes/2026-05-04_engineer_overhaul.md` and
`docs/changes/2026-05-05_*` (when written) for the full v2 design.
