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
- port: exposed port number (if applicable)
- depends_on: list of other service names this depends on
- requirements: list of pip/npm packages needed (e.g. ["fastapi", "uvicorn", "pydantic"] for Python, or ["react", "axios"] for TypeScript)

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
- Define proper port mappings, environment variables, and dependencies
- Include volume mounts for development (live code reloading)
- Use a shared network for inter-service communication

Example docker-compose service for a Python backend:
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
"""
