# Engineer Overhaul — Plan

**Date:** 2026-05-04
**Branch:** `feat/engineer-overhaul` (off main)
**Restore point:** `restore/pre-engineer-overhaul`

## Why

The engineer dispatches per-issue coders that work in isolation. Each
coder writes its files without seeing what other coders in the same
service produced. Result: internally inconsistent code that compiles
in pieces but doesn't fit together.

We saw this concretely on property_manager M1:

- `LoginPage.tsx` was written assuming `useAuthStore` exposes a
  `login()` method.
- `authStore.ts` was written without that method (only `setSession`,
  `hydrate`, `logout`).
- TypeScript would catch the mismatch, but `tsc` is never run.
- Integration tests catch it at runtime — but only after we've spent
  $0.50 on a full milestone build.

The same class of bug also surfaced cross-service (frontend sent
`{username, password}` while backend expected `{email, password}`).
We patched that one by stopping schema-stripping in the
backend → frontend prompt handoff. The within-service version of the
same bug is still wide open.

## Core insight

> Each service has a contract. The documenter extracts it.
> Downstream consumers read it. Tests validate it.

This abstraction generalizes to backends, frontends, workers, queues,
databases, libraries. What changes per service-type is *what the
contract is* and *how to extract it*. The architecture itself stays
framework-agnostic; framework-awareness lives in **service-type
profiles**.

## Two scopes, one mechanism

Coders need two kinds of contract context, ALWAYS BOTH:

| Scope | What | Extracted by |
|---|---|---|
| Within-service | Exports of files this coder will import (e.g. authStore) | Language AST tooling (ts-morph, Python ast) |
| Cross-service | API surfaces of services this code will call (e.g. backend OpenAPI) | Already done — runtime capture from FastAPI etc. |

The injection mechanism is uniform: when a coder is about to write
file X, look at X's intended imports and HTTP calls; find each dep's
contract; paste the relevant slice into the prompt. The mechanism
doesn't care whether the source is TypeScript-AST-extracted or
OpenAPI-runtime-captured.

## Docs layout (service-first)

```
docs/
├── architecture/                    project-level (services list, ADRs, milestones)
├── runs/                            cross-service run reports (already exists)
├── memory/                          portable auto-memory (already exists)
├── changes/                         session narratives (already exists)
├── backend/
│   ├── api.openapi.json             runtime-captured
│   ├── code/                        Python-AST-extracted
│   │   ├── routes.json
│   │   ├── schemas.json
│   │   └── deps.json
│   └── tests/
│       └── integration-results.json cached for layer-transition gate
├── frontend/
│   ├── code/                        TypeScript-AST-extracted
│   │   ├── api.d.ts                 exported function signatures
│   │   ├── stores.json              zustand/redux store shapes
│   │   └── routes.json              src/routes/*.tsx → route paths
│   └── tests/
│       └── integration-results.json
└── auth/
    └── AUTH_CONTRACT.md             already exists
```

Service-first because:
- An engineer working on `frontend` should look in `docs/frontend/`
  symmetric with its workspace at `frontend/`.
- Evolve mode becomes nearly free: `ls docs/` shows what already
  exists; `ls docs/<service>/code/` shows what's been extracted.
- Project-level artifacts (`docs/architecture/`, `docs/runs/`)
  remain at the top level because they are genuinely cross-service.

## Service-type profile registry

```python
SERVICE_PROFILES = {
    "backend.fastapi": {
        "documenter": PythonAstDocumenter,
        "validator": "pytest tests/ -q",
        "contract_format": "openapi",
        "skeleton": "fastapi",
    },
    "frontend.react": {
        "documenter": TypeScriptAstDocumenter,
        "validator": "tsc --noEmit",
        "contract_format": "typescript-d-ts",
        "skeleton": "react",
    },
    "worker.python": {
        "documenter": PythonHandlerDocumenter,
        "validator": "pytest tests/",
        "contract_format": "event-schema",
        "skeleton": "consumer-python",
    },
    "queue.redis": {
        "documenter": ConfigSnapshotDocumenter,
        "validator": "redis-cli ping",
        "contract_format": "topic-list",
        "skeleton": None,
    },
    # ...
}
```

Architect, engineer, integration runner stay framework-agnostic.
They look up the profile by `(service_type, framework)` and use
whatever tooling that profile prescribes.

**Guardrail:** if the planner emits a `(service_type, framework)`
combination with no profile entry, the planner output is rejected
and the planner is asked to redo with a known combination. Prevents
silent miscoupling on new service types we haven't profiled.

## Phases (priority order)

| Phase | Work | Leverage |
|---|---|---|
| 0 | This plan doc + branch + commit current session work | — |
| 1 | TypeScript + Python documenters → `docs/<service>/code/` | High — solves today's bug |
| 2 | Inject deps' interfaces into coder prompt (within-service) | High — bug class disappears |
| 3 | Layer-transition gate: backend integration tests must pass before frontend dispatch | High — prevents building against broken backend |
| 4 | Reshape `docs/` to service-first layout (migrate existing artifacts) | Medium — discoverability |
| 5 | Service-type profile registry; migrate hardcoded behavior to profile lookups | Medium — scales beyond fastapi+react |
| 6 | Engineer pre-flight (validate deps + docs) + post-flight (run validator, regenerate on fail) | High — catches type errors before integration |
| 7 | Architect reads workspace state in evolve mode | Medium — only matters M2+ |

Phases 1–3 are the leverage. Each ships independently, gets
validated against M1, and we don't move on until it converges.
Phases 4–7 are the scaling layer.

## Validation strategy

Each phase ends with:

1. Unit tests for new code.
2. Re-run M1 (`./tests/e2e/property_manager/run.sh m1`).
3. Confirm the specific failure mode the phase targets is gone.
4. Capture cost and elapsed; compare against prior baseline.
5. Commit with a message naming what changed and what's verified.

If a phase doesn't converge after two iterations, stop and reassess
before continuing.

## Out of scope

- Codegen typed API client from OpenAPI (task #17) — defer to after
  the structural overhaul is in.
- Orchestrator stall on duplicate jest configs (task #18) —
  orthogonal; address when it surfaces again.
- UX designer post-milestones phase relocation (tasks #9, #10) —
  can land independently of the overhaul.

## Branch lifecycle

- `restore/pre-engineer-overhaul` — pinned snapshot of state before
  Phase 1. Never touched. Push to remote. Permanent rollback target.
- `feat/engineer-overhaul` — where the work happens. Pushed to
  remote so the user can see progress.
- When the overhaul is fully validated end-to-end on M1 (and ideally
  M1 → M2), merge `feat/engineer-overhaul` → `main`. The restore
  branch stays around indefinitely.
