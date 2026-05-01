---
name: Skeleton repos for project seeding
description: Four bizniz-skeleton-* repos cloned to ~/, used to seed services with batteries-included starting points
type: reference
originSessionId: 44c643bd-6fd0-4168-b18b-8f23a5343205
---
Four skeleton repos live at `/home/jamey/bizniz-skeleton-{fastapi,react,angular,teams}/`. Each is a real GitHub repo at `github.com/coldicefisher/bizniz-skeleton-<name>` with a working baseline (auth, Docker, tests, README).

Intended usage per the architect:
- **fastapi** → backend services (Python/FastAPI). Has full auth system + tests + Docker Compose.
- **react** → general frontend services (React/TypeScript/Vite). Has tests + Docker.
- **angular** → dashboard-heavy frontend (Material + NgRx + theming). Has tests + Docker.
- **teams** → system template for fan-out / realtime feed architectures (Microsoft Teams-like). Already multi-service: backend/consumer/frontend-angular/infra.

Selection rules (per the user, 2026-04-29):
- Two frontends available — angular for dashboard-intensive UIs, React for everything else.
- FastAPI for backends.
- Teams when the system needs realtime fan-out feeds.

The architect should pick the right skeleton in its decomposition step and seed it into the service workspace BEFORE Docker build, so the engineer only has to add the app-specific code on top.

This wiring is **not yet committed** — both `main` and `refactor/agent-specialization` lack the skeleton-selection prompt and the seed-from-skeleton step.
