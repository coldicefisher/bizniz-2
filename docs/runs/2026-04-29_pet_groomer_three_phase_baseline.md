# Run — Pet Groomer (three-phase baseline)

- **Date:** 2026-04-29
- **Branch:** `refactor/agent-specialization`
- **Head commit at run time:** `b08bbd4` (Engineer: three-phase strategy)
- **Result:** **8/8 issues PASS** in **21.5 minutes** (1294s)
- **Cost:** unmeasured (cost tracking is plan item #5; not yet implemented)

## Problem statement

Build a web application for a pet grooming salon.
- Customers can view services with prices, book appointments, and view/cancel existing appointments.
- Backend is a REST API with services, appointments, basic validation (no double-booking).
- In-memory storage; no database required.

## Architecture

The architect decomposed the problem into two services:

| Service | Type | Framework | Language | Skeleton seeded | Container port |
|---|---|---|---|---|---|
| backend | backend | fastapi | python | `fastapi` | 8000 |
| frontend | frontend | react | typescript | `react` | 5173 |

Cleanup at start of run: removed 0 stale images (fresh state).
Free-port allocation: no collisions, no remaps.

### Backend engineering plan

- Package: `pet_groomer`
- Namespaces: 3
- Domain models: 2 (Service, Appointment)
- Modules: 2
- Issues: 5 across 3 dependency layers

### Frontend engineering plan

- Package: `pet-groomer-frontend`
- Namespaces: 4
- Domain models: 2
- Modules: 2
- Issues: 3 across 3 dependency layers

## Models config (bizniz.yaml at run time)

```yaml
default_model:        gemini-flash-lite
engineer_model:       gemini-flash
architect_model:      gemini-flash
coder_models:     [gemini-flash-lite, gemini-flash, gemini-pro]
tester_models:    [gemini-flash-lite, gemini-flash, gemini-pro]
repair_models:        [gemini-flash, gemini-pro]
debugger_model:       gemini-pro
debugger_max_iterations: 12
stall_threshold:      3
agentic_debug_threshold: 2
enable_agentic_debug: false   # Phase 3 auto-enables regardless
max_iterations:       20
```

## Three-phase strategy mapping

- **Phase 1 (frame):** `gemini-flash-lite`, no tests, no Docker, 1 shot per ticket
- **Phase 2 (escalate):** sub-pass per model in `coder_models[1:]` over still-failing tickets
  - Sub-pass A: `gemini-flash`, `max_iterations=2` per ticket
  - Sub-pass B: `gemini-pro`, `max_iterations=2` per ticket
- **Phase 3 (debug):** `gemini-pro` + agentic debugger (full tools), `max_iterations=12` per ticket
  - Not triggered this run

## Per-issue breakdown — backend

| # | Issue | Layer | Phase 1 frame | Phase 2 — flash | Phase 2 — pro | Phase 3 | Solved by |
|---|---|---|---|---|---|---|---|
| 1 | Define Service Model | L0 | ✓ 2 files | ✗ regression | ✓ iter 2 | — | gemini-pro |
| 2 | Define Appointment Model | L0 | ✓ 1 file | ✓ iter 1 | — | — | gemini-flash |
| 3 | Create FastAPI Application Factory | L0 | ✓ 1 file | ✓ iter 2 (collection-err repair) | — | — | gemini-flash |
| 4 | Implement DataStore Repository | L1 | ✓ 2 files | ✓ iter 2 | — | — | gemini-flash |
| 5 | Implement API Routes | L2 | ✓ 2 files | ✗ | ✓ iter 1 | — | gemini-pro |

- Phase 1 framing: 5/5 OK in 47s
- Phase 2 flash: 3/5 passed (issues 2, 3, 4)
- Phase 2 pro: 2/2 passed (issues 1, 5)
- Phase 3: not needed
- Skeleton baseline tests passing throughout: 14–15 (FastAPI auth suite)

## Per-issue breakdown — frontend

| # | Issue | Layer | Phase 1 frame | Phase 2 — flash | Phase 2 — pro | Phase 3 | Solved by |
|---|---|---|---|---|---|---|---|
| 1 | Define Domain Models | L0 | ✓ 1 file | ✓ iter 1 | — | — | gemini-flash |
| 2 | Implement API Client | L1 | ✓ 2 files | ✗ regressions, iter 2 fail | ✓ iter 1 | — | gemini-pro |
| 3 | Build Service List Component | L2 | ✓ 1 file | ✓ iter 1 | — | — | gemini-flash |

- Phase 1 framing: 3/3 OK in 25s
- Phase 2 flash: 2/3 passed (issues 1, 3)
- Phase 2 pro: 1/1 passed (issue 2)
- Phase 3: not needed
- Skeleton baseline tests passing throughout: 10–12 (React/auth suite)

## What worked

- **Skeleton seeding** — both services started from working baselines (FastAPI auth + tests; React + auth flow + jest config). 14–15 backend / 10–12 frontend tests already green at Phase 2 entry.
- **Phase 1 framing on the cheapest tier** — populated the workspace with real implementations before any test ran, so Phase 2 had coherent imports to work with.
- **Phase 2 escalation — flash → pro** — caught the easy 5/8 on flash, the hard 3/8 on pro. No model wasted on tickets it can't solve.
- **Config-aware repair** — when a repair attempt needed `package.json` or jest config edits, the auto-loaded config files (commit 6be43e3) made those edits land instead of being filtered.
- **`/workspace/node_modules` symlink to `/app/node_modules`** (commit c15c2d3) — React skeleton's pre-built deps now visible to jest in the test container. Frontend ran with no missing-package stalls.
- **Topological framing order** — issue 5 (API Routes, L2) saw real Service/Appointment/AppFactory/DataStore in the workspace when its codegen ran; cross-issue imports resolved naturally.

## What didn't work / friction points

- **Issue 1 backend regression** (Phase 2 flash) — the AI's repair pass introduced a regression in `tests/models/test_service.py` it couldn't fix in 2 iterations. Recovered on the gemini-pro sub-pass.
- **Issue 2 frontend regressions** (Phase 2 flash) — new API client shadowed 3 skeleton auth tests; flash repair stuck on a Node deprecation warning (DEP0040 punycode). Pro fixed it cleanly.
- **Phase 3 never triggered** — meaning we don't have a real-world data point on the agentic debugger path yet.

## Compare with prior runs

| Run | Branch / commits | Backend | Frontend | Total | Notes |
|---|---|---|---|---|---|
| OpenAI baseline | `main` (e3c7c41) | killed mid-Layer 2 | n/a | n/a | Collection-error miscls + token limits |
| Gemini, no skeletons | `refactor/agent-spec` (aa91e7b) | died Layer 0 in tool loop | n/a | n/a | JSON \escape bug |
| Gemini + skeletons (no framing) | `5a95f97` | 2/3 issues | 3/3 issues | ~12 min | Layer 0 lost to JSON \escape |
| Gemini + skeletons + framing | `483631b` | 3/3 (but +JSON-fix) | partial | ~22 min stopped | Frontend stuck on jest deps |
| Gemini + skeletons + framing + npm fix + 3-phase (this run) | **`b08bbd4`** | **5/5** | **3/3** | **21.5 min** | First clean end-to-end |

## Open questions / next levers

- **Cost numbers** — plan item #5: capture token usage per AI call, hardcode a `MODEL_PRICING` table, roll up via `Project.cost_summary()`. Today this run cost $? — unmeasured.
- **Phase 3 calibration** — the run never hit it, so debugger_max_iterations=12 is a guess. Run a deliberately harder problem to exercise the agentic path and tune.
- **Regression handler vs. Phase 2 caps** — `max_iterations=2` per ticket per phase model is tight. A regression repair eats one of those iterations, leaving only one for the actual issue. Could allow regression-recovery iterations to not count against the cap.
- **Engineer doesn't know about skeletons** — `analyze()` regenerates issues for things the skeleton already provides (e.g., the FastAPI skeleton has its own auth). The framing pass + test loop catch most of this, but tighter scoping would shave time.
