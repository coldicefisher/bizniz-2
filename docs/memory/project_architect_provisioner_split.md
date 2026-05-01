---
name: Architect / Provisioner split
description: AutoArchitect plans only; Provisioner materializes (templates, compose, FusionAuth default)
type: project
originSessionId: 44c643bd-6fd0-4168-b18b-8f23a5343205
---
The pipeline now has a clean planning/materialization split.

**Architect** (`bizniz/architect/auto_architect.py`):
- `decompose()` is the only AI call. Returns `SystemArchitecture`.
- `build()` is a thin shell: `decompose() → Provisioner.provision() → dispatch engineers`.
- Prompt no longer emits docker-compose YAML.
- `AutoArchitectSchema` no longer requires `docker_compose`; `SystemArchitecture.docker_compose` is optional and only used for the human-readable architecture.md preview.

**Provisioner** (`bizniz/provisioner/`):
- `Provisioner.provision(architecture, project_name)` does ports + cleanup + skeletons + templates + compose + .env + image builds.
- Templates registered: `postgres`, `redis`, `fusionauth`, plus sentinels `__python_app__` / `__typescript_app__` for generic app services without skeletons.
- `compose_builder.build_compose()` produces YAML deterministically from the architecture; no AI in the materialization path.
- `Provisioner(build_images=False)` for tests / CI.

**FusionAuth as default OAuth.** When the architect's plan includes user accounts, prompt instructs it to add a `fusionauth` auth service AND a `postgres` database service. Provisioner ships:
- `fusionauth/kickstart/kickstart.json` with default tenant, application named after the project, admin + user roles, OAuth redirects for both React (5173) and Angular (4200) frontends, JWT settings, an initial admin user, and a bootstrap API key.
- `postgres/init.sql` that creates the `fusionauth` DB alongside the app DB.
- `.env` contributions: `FUSIONAUTH_API_KEY`, `FUSIONAUTH_ISSUER`, `FUSIONAUTH_APPLICATION_ID`, etc.

**Why:** the old monolithic architect was conflating planning, infra provisioning, and orchestration. Templates + deterministic compose remove a class of LLM bugs and make multi-week projects (where you re-provision an existing project for a new milestone) tractable. The Planner agent (work item not yet started) will sit above the architect.

**Tests:** 74 unit tests + 2 functional tests (real Gemini) all green. 589 non-functional tests pass on main.

**Commits:** `6168282` (split + templates), `960b4cb` (merge to main). All on `main`, 17 commits ahead of `origin/main` and not yet pushed.

**How to apply:** When designing future work that touches infra (new template, milestone planner, cost-aware provisioning), start in `bizniz/provisioner/`. When designing AI-prompt work or service-decomposition logic, start in `bizniz/architect/`. Don't put AI calls in the provisioner; don't put file writes in the architect.
