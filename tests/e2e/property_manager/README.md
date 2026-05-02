# Property Manager — E2E Lifecycle Test

First full-lifecycle test of the bizniz pipeline. This exercises
every layer: planner → architect → provisioner → engineer →
integration tester → agentic debugger, across multiple milestones
with real infrastructure (Postgres, JWT auth).

## Problem Statement

See `problem_statement.txt`. A property management app for small
landlords with:
- Tenant management (CRUD, lease tracking)
- Rent collection (manual payments, overdue detection)
- Maintenance requests (ticketing with comments + status)
- JWT auth with landlord/tenant roles

## Expected Milestones

The planner should produce ~3-5 milestones, roughly:
1. Auth + tenant/property CRUD (greenfield — backend + frontend + DB)
2. Rent collection (evolve — extend backend + frontend)
3. Maintenance requests (evolve — extend backend + frontend)
4. Polish / cross-cutting (notifications, overdue logic)

The exact decomposition depends on the planner model's output.

## Running

```bash
# From repo root, always:
cd ~/bizniz && set -a && source .env && set +a

# Plan (cheap — ~$0.01)
./tests/e2e/property_manager/run.sh plan

# Execute M1 (greenfield build — $1-3, 15-40 min)
./tests/e2e/property_manager/run.sh m1

# Verify M1: stand it up and poke around
./tests/e2e/property_manager/run.sh up
# → http://localhost:8000/docs  (API)
# → http://localhost:5173       (frontend)
./tests/e2e/property_manager/run.sh down

# Run integration tests against M1
./tests/e2e/property_manager/run.sh integration

# Execute M2 (evolve — extends existing services)
./tests/e2e/property_manager/run.sh m2
```

## What Success Looks Like

### M1 (Greenfield)
- Backend: FastAPI + Postgres + Alembic migrations
- Frontend: React with routing
- Auth: JWT login/register, role middleware
- CRUD: Properties and tenants
- Integration tests: pass against live stack
- Docker: `docker compose up` works, DB initializes

### M2+ (Evolve)
- Existing services preserved (code not wiped)
- New endpoints added to existing backend
- Frontend extended with new pages/routes
- Integration tests cover new + existing functionality
- No regressions on M1's integration tests

## Files

| File | Purpose |
|---|---|
| `problem_statement.txt` | The natural-language input |
| `run.sh` | Convenience runner |
| `README.md` | This file |

## Project Output

Generated project lands at: `~/bizniz_projects/property_manager_v1/`

```
property_manager_v1/
├── backend/          (FastAPI + SQLAlchemy + Alembic)
├── frontend/         (React + TypeScript)
├── infra/development/
│   ├── docker-compose.yml
│   ├── .env
│   ├── backend/Dockerfile
│   └── frontend/Dockerfile
├── contracts/        (captured OpenAPI specs)
├── docs/
│   ├── plan.json     (saved milestone plan)
│   ├── architecture.md
│   └── runs/         (per-run efficiency reports)
└── .bizniz/project.db
```
