from bizniz.architect.skeletons import skeletons_summary_for_prompt


DECOMPOSE_PROMPT_TEMPLATE = """\
Problem statement:
{problem_statement}

Project name: {project_name}

Decompose this into a service-based architecture. For each service, specify:
- name: short identifier (e.g. "backend", "frontend", "db")
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
- docker_compose: a complete docker-compose.yml file content as a string

IMPORTANT framework rules:
- Backend: ALWAYS use Python with FastAPI unless client explicitly requests otherwise
- Frontend apps: Use React with TypeScript
- Dashboard apps: Use Angular with TypeScript
- NEVER use Node.js for backends
- C#/.NET is ONLY for enterprise refactors, never for new projects

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
- "none" only for infrastructure services (postgres/redis/nginx) or when no skeleton fits.

Container-port reference (use these on the right side of port mappings when the service has a skeleton):
- fastapi → 8000   |   teams-backend → 8000
- react → 5173
- angular → 4200   |   teams-frontend → 4200
- teams-consumer → no port (worker)

Project directory structure:
Source code lives at project_root/<service>/, Docker configs at infra/development/:
  project_root/backend/         <- source code + requirements.txt
  project_root/frontend/        <- source code + package.json
  project_root/infra/development/backend/   <- Dockerfile
  project_root/infra/development/frontend/  <- Dockerfile
  project_root/infra/development/docker-compose.yml
  project_root/infra/development/.env

The docker-compose.yml must:
- Use build contexts pointing to "../../<workspace_name>" for application services (source code root)
- Reference Dockerfiles at "../../infra/development/<workspace_name>/Dockerfile"
- Use standard Docker Hub images for infrastructure (postgres, redis, etc.)
- Define proper port mappings as "<host>:<container>" — pick a sensible host port for dev,
  and use the container-port reference above for the right side when a skeleton is in use.
  The host port may be re-allocated if it collides with something else on the dev machine.
- Include volume mounts for development (live code reloading)
- Use a shared network for inter-service communication

Example docker-compose service for a Python/FastAPI backend with skeleton=fastapi:
```yaml
  backend:
    build:
      context: ../../backend
      dockerfile: ../../infra/development/backend/Dockerfile
    ports:
      - "8000:8000"
    volumes:
      - ../../backend:/app
    environment:
      - DATABASE_URL=postgresql://user:pass@db:5432/dbname
    depends_on:
      - db
    networks:
      - app-network
```
""".replace("{skeletons}", skeletons_summary_for_prompt())
