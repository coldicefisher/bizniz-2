from bizniz.architect.skeletons import skeletons_summary_for_prompt


DECOMPOSE_PROMPT_TEMPLATE = """\
Problem statement:
{problem_statement}

Project name: {project_name}

Decompose this into a service-based architecture. For each service, specify:
- name: short identifier (e.g. "backend", "frontend", "db", "auth")
- service_type: one of "backend", "frontend", "database", "cache", "proxy", "worker", "auth"
- framework: the framework to use (e.g. "fastapi", "react", "angular", "nginx", "postgres", "redis", "fusionauth")
- language: primary language ("python", "typescript", "yaml", "sql")
- description: what this service does
- workspace_name: directory name for the service source code (e.g. "backend", "frontend")
- port: exposed HOST port number (the dev port on the host machine, if applicable)
- depends_on: list of other service names this depends on
- requirements: list of pip/npm packages needed (e.g. ["fastapi", "uvicorn", "pydantic"] for Python, or ["react", "axios"] for TypeScript)
- skeleton: which skeleton repo to seed this service from (pick from the list below, or "none")

Also provide:
- project_name: the human-readable project name
- project_slug: the slugified project name (e.g. "{project_slug}")
- description: overall system description

You do NOT generate docker-compose.yml. The Provisioner builds compose
deterministically from your service list and the registered infrastructure
templates (postgres, redis, fusionauth).

IMPORTANT framework rules:
- Backend: ALWAYS use Python with FastAPI unless client explicitly requests otherwise
- Frontend apps: Use React with TypeScript
- Dashboard apps: Use Angular with TypeScript
- NEVER use Node.js for backends
- C#/.NET is ONLY for enterprise refactors, never for new projects

Authentication (REQUIRED whenever the project has user accounts, login,
or any concept of "user"):
- The pipeline supports exactly TWO auth modes: **FusionAuth** or **none**.
  No custom JWT signing, no Auth0/Cognito/Keycloak/Clerk, no DIY session
  cookies, no application-side password hashing. If the problem implies
  any user identity, you provision FusionAuth. If it doesn't, you skip
  the auth service entirely. There is no third option.
- When auth is needed: add an auth service with framework="fusionauth",
  service_type="auth", language="yaml", workspace_name="fusionauth",
  port=9011, skeleton="none".
- FusionAuth REQUIRES postgres. If you add a fusionauth service, you MUST
  also add a postgres service: framework="postgres", service_type="database",
  language="sql", workspace_name="postgres", port=5432, skeleton="none".
- The Provisioner ships a kickstart.json rendered from the milestone's
  cumulative AuthSpec (roles, applications, groups, test users seeded
  from the planner's auth_delta deltas, plus an always-seeded admin
  with passwordChangeRequired=true). You don't plan any FusionAuth
  config yourself — the spec → kickstart pipeline handles it.
- Backend services that need auth should list "auth" in their depends_on.
- Backend code validates FusionAuth-issued JWTs via JWKS and reads roles
  from the JWT's ``roles`` claim. There is no local Role/UserRole table
  in the skeleton — engineers MUST NOT introduce one.

Available skeletons (pre-built starter repos that come with auth, Docker, tests, README):
{skeletons}

Skeleton selection rules:
- Pick the matching skeleton for any application service (backend, frontend, worker) so it
  starts with a real working baseline instead of from scratch.
- "fastapi" for Python/FastAPI backends.
- "react" is the DEFAULT frontend.
- "angular" only when the UI is dashboard-heavy / data-dense (admin consoles, BI dashboards).
- The "teams-*" skeletons go together as a 3-service system pattern when the problem requires
  realtime fan-out feeds (Microsoft Teams-like channels, activity streams, group chat, etc.).
  When you pick the teams pattern, generate exactly three services: one with skeleton=teams-backend,
  one with skeleton=teams-consumer, one with skeleton=teams-frontend.
- Infrastructure services (database, cache, proxy, auth) ALWAYS use skeleton="none" — the
  Provisioner has dedicated templates for them.

Container-port reference (used by the Provisioner's compose builder; you only need to
set the HOST port via service.port — the Provisioner picks the container side):
- fastapi → 8000   |   teams-backend → 8000
- react → 5173
- angular → 4200   |   teams-frontend → 4200
- teams-consumer → no port (worker)
- fusionauth → 9011  |  postgres → 5432  |  redis → 6379

Project directory layout the Provisioner produces (informational — you don't write these):
  project_root/<workspace_name>/    <- source code per app service (skeleton or generated)
  project_root/infra/development/<workspace_name>/Dockerfile
  project_root/infra/development/postgres/init.sql
  project_root/infra/development/fusionauth/kickstart/kickstart.json
  project_root/infra/development/docker-compose.yml
  project_root/infra/development/.env
""".replace("{skeletons}", skeletons_summary_for_prompt())
