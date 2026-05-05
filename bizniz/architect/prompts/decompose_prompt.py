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
- The pipeline always delegates identity to a managed provider — never a
  custom in-application auth implementation. No application-side password
  hashing, no DIY session cookies, no hand-rolled JWT signing.
- DEFAULT: add a FusionAuth service. Switch to a different managed
  provider only when the problem statement names an EXPLICIT CONSTRAINT
  (existing customer Okta tenant, regulatory requirement to use AWS
  Cognito, vendor contract mandating Auth0, etc.). Without an explicit
  constraint, default to FusionAuth.
- When the problem doesn't imply any user identity, skip the auth service
  entirely.
- When auth is needed (FusionAuth default): add an auth service with
  framework="fusionauth", service_type="auth", language="yaml",
  workspace_name="fusionauth", port=9011, skeleton="none".
- FusionAuth REQUIRES postgres. If you add a fusionauth service, you MUST
  also add a postgres service: framework="postgres", service_type="database",
  language="sql", workspace_name="postgres", port=5432, skeleton="none".
- A separate AuthAgent runs after this Architect call. It reads the
  milestone's problem_slice and materializes the identity state (roles,
  applications, test users) via the configured auth provider's API. You
  do NOT specify roles, applications, or test users in your output —
  only that the auth service exists in the architecture.
- Backend services that need auth should list "auth" in their depends_on.
- Backend code validates JWTs issued by the auth provider via JWKS and
  reads roles from the JWT's ``roles`` claim. There is no local
  Role/UserRole table in the skeleton — engineers MUST NOT introduce one.

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
