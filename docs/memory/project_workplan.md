---
name: Bizniz auto-engineering work plan
description: Status of the four-priority work plan (originally 2026-04-29; resynced 2026-04-30)
type: project
originSessionId: 44c643bd-6fd0-4168-b18b-8f23a5343205
---

Status as of 2026-04-30 (resynced — most items shipped):

1. **Skeleton wiring** — ✅ DONE. Auto-clone from `github.com/coldicefisher/bizniz-skeleton-*`, Provisioner probe → reconcile → materialize, AI escape hatches (`ai_fallback`, `ai_recovery`), saas bundle (`bizniz-skeleton-saas`: api + websocket-server + store-consumer + frontend + vendored core). Smoke tests (no-AI + heavy AI) both pass. Multiple compose-generation bugs caught and fixed during smoke runs.

2. **Quick-pass into engineer path** — ✅ DONE. `bizniz/engineer/framing.py` is the Phase-1 framing pass; `Engineer.run_layered(framing=True)` runs it before the layer loop; `Engineer.run_three_phase()` adds Phase-2 escalation chain across model tiers. The "engineer-side port of `examples/codegen_blast.py`" is in place.

3. **Two pipeline bugs** (collection-error misclassification, read-only filter blocks config fixes) — ❓ likely partly obsolete. Original work plan said "Don't fix prematurely — they may go away once skeletons are wired" because the FastAPI/React skeletons ship correct app.py imports + jest config. Skeletons ARE wired. If these resurface on a non-skeleton path or unusual case, revisit; otherwise leave as known edge cases. See `project_pipeline_bugs.md`.

4. **Docs** — ✅ DONE. `docs/home.md`, `docs/pipeline_sequence.md`, per-role docs (`docs/roles/`), per-module references (`docs/modules/`), architecture (`docs/architecture/`) including `architect_provisioner_split.md`, `cost_tracking.md`, `error_classification.md`, `evolve_mode.md`, `run_reports.md`. ~30+ docs.

5. **Cost analysis** — ✅ DONE. `bizniz/cost/` with `MODEL_PRICING`, `CostTracker`, capture in all 3 clients, by-model + by-agent rollup, run-end summary. Persistence via `ProjectDB.save_api_call` (architect.py:444 and :718). 31 cost tests pass. Doc at `docs/architecture/cost_tracking.md`.

6. **Per-run efficiency doc** — ✅ DONE 2026-04-30 (commit `02ecb74`). `bizniz/run_report/` writes `<project_root>/docs/runs/<job_id>.{md,json}` from `Architect.build()` finally-block. Includes architecture summary, models snapshot, per-service results, cost roll-up, and "delta since last run" computed from previous JSON sidecar. 11 unit tests. Doc at `docs/architecture/run_reports.md`.

**What's left:** Effectively nothing from the original plan. Possible follow-ups:
- Verify Bug 1 / Bug 2 are dead (or fix them if they resurface).
- Heavy AI smoke test for the saas bundle (the existing heavy smoke uses a CRM prompt without realtime/long-running language, so the architect picks `fastapi`+`react`. A SaaS-shaped prompt would exercise the `saas-*` selection path).
- Whatever the user defines next.

**How to apply:** When the user picks up a session and asks "where were we", everything from the original 4-priority plan is done. Pick a fresh direction with them.
