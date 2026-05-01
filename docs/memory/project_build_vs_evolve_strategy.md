---
name: Build vs evolve — two-mode pipeline strategy
description: Greenfield handoff with spec-driven build, then evolve mode with discovery tools anchored on v0 artifacts
type: project
originSessionId: 44c643bd-6fd0-4168-b18b-8f23a5343205
---
Strategic direction set 2026-05-01 for how bizniz handles large/long-lived apps.

**The two modes:**

1. **Build mode (current `architect.build()`):** spec-driven greenfield. Architect → engineer per service → integration phase. Works for new apps with up to ~30-50 services. The pet_groomer pipeline is this mode.

2. **Evolve mode (stubbed `architect.evolve()`, not built yet):** for adding features to an already-built app. Reads v0 artifacts as anchors, uses discovery tools (grep/read/find-references) to navigate existing code, dispatches engineers to ADD without rewriting.

**The four v0 artifacts that bridge the modes:**
1. Architecture digest JSON (project DB / per-run JSON sidecar)
2. Integration tests against the live stack (`tests/integration/test_*_api.py` — these become the executable spec)
3. `SKELETON.md` per service (directory contract)
4. Captured OpenAPI/contract files (`<project_root>/contracts/<svc>.openapi.json`)

Together these are what evolve-mode agents read FIRST. They run the integration tests as a baseline before touching anything; that protects against silent regressions.

**Why this works:**
- Greenfield is easier for spec-driven (no prior code to clash with)
- Integration tests pin behavior — they're the durable contract, not decoration
- Discovery tools (grep workspace, find references, list dir) fill the gap between "what the digest claims" and "what the code does today"
- Same pattern Claude Code itself uses internally — read artifacts, fall back to grep

**Implications for what we build now:**
- Integration test quality is load-bearing. Bad v0 tests = misleading v1 agents. The integration phase shipped 2026-05-01 (`bizniz/integration/`) is what makes the whole evolve-mode plan viable.
- Don't build evolve-mode discovery tooling yet — premature until we have a real evolving app.
- DO save the four artifacts cleanly so they're trivially findable later.

**Why:** The user's discoverer→business_manager→builder pipeline produces apps that customers iterate on. If bizniz only does greenfield, customers are stuck or have to manually touch code. The build/evolve seam means bizniz can hand off a v0 AND keep adding features against it later via the same machinery.

**How to apply:**
- For any "scale this up" request: don't add discovery tooling until evolve mode is on the roadmap.
- For new pipeline work: ensure outputs are captured into the four artifacts so future evolve mode has anchors.
- For sockets/realtime apps: route to the saas skeleton (already ships the realtime backbone). v0 integration tests for WebSocket flows are non-negotiable — they're the contract evolve mode will trust.
- When a customer asks for feature X on an existing app, that's the trigger to build evolve mode — not before.
