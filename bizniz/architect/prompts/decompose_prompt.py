DECOMPOSE_PROMPT_TEMPLATE = """\
Problem statement:
{problem_statement}

Project name: {project_name}

Decompose this into a service-based architecture. For each service, specify:
- name: short identifier (e.g. "api", "frontend", "db")
- service_type: one of "backend", "frontend", "database", "cache", "proxy", "worker"
- framework: the framework to use (e.g. "fastapi", "angular", "nginx", "postgres", "redis")
- language: primary language ("python", "typescript", "yaml", "sql")
- description: what this service does
- workspace_name: slug for the workspace (e.g. "{{project_slug}}_backend", so for this project: "{project_slug}_backend")
- port: exposed port number (if applicable)
- depends_on: list of other service names this depends on

Also provide:
- project_name: the human-readable project name
- project_slug: the slugified project name
- description: overall system description
- docker_compose: a complete docker-compose.yml file content as a string

The docker-compose should define all services with appropriate build contexts,
ports, environment variables, volumes, and dependencies.
For application services (backend, frontend), use build context pointing to
"./{{workspace_name}}" directories. For infrastructure services (db, redis),
use standard Docker Hub images.
"""
