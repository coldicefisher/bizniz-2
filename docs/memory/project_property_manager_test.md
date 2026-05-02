---
name: Property Manager E2E lifecycle test
description: First full-lifecycle test — Postgres, JWT auth, 5 milestones, milestone-driven build with integration tests per milestone
type: project
---

Property Manager V1 is the first real full-lifecycle test of the pipeline. Problem: property management for small landlords (Postgres, JWT auth, two roles, 4 domains).

Planner produced 5 milestones (2026-05-02): Auth → Properties → Tenants/Leases → Rent → Maintenance.

**Why:** The pet-groomer tests (V10/V11) validated the pipeline mechanics (engineering, integration tests, debugger). Property Manager validates the full lifecycle: planner → milestone-driven evolve → real DB + auth → integration tests per milestone → human verification gates.

**How to apply:** Tests live in `tests/e2e/property_manager/`. Run via `run.sh plan|m1|m2|integration|up|down`. The script uses `examples/milestone_build.py` which calls `architect.build_with_plan()`. Integration tests run after each milestone automatically. Plan is saved to `~/bizniz_projects/property_manager_v1/docs/plan.json` for resume.
