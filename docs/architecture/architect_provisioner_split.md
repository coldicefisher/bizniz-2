# Architect / Provisioner split

The architect plans the system. The provisioner materializes it.

```
problem statement
       │
       ▼
┌────────────────────┐
│   Architect    │   pure planning — one AI call
│   .decompose()     │   returns: SystemArchitecture
└──────────┬─────────┘
           │ (services, frameworks, ports, depends_on, skeleton)
           ▼
┌────────────────────┐
│   Provisioner      │   no AI — pure materialization
│   .provision()     │   returns: ProvisionResult
└──────────┬─────────┘
           │
           ├── allocate free host ports (avoid dev-machine collisions)
           ├── clean up leftover images + containers from prior runs
           ├── create project_root/, infra/development/, per-service workspaces
           ├── per service:
           │       ├─ infrastructure  → render template (postgres, redis, fusionauth)
           │       │                    write infra files (init.sql, kickstart.json)
           │       └─ application
           │           ├─ skeleton    → seed from ~/bizniz-skeleton-*
           │           └─ generated   → render PythonAppTemplate / TypeScriptAppTemplate
           ├── build docker-compose.yml deterministically
           ├── build .env from template-contributed env_vars
           └── docker build for each app service (when build_images=True)
           ▼
┌────────────────────┐
│   Engineer     │   per app service — analyze + frame + dispatch
│   .run_three_phase │
└────────────────────┘
```

## Why split

Before the split, `Architect.build()` was three concerns rolled into
one class: planning, infra provisioning, and engineer orchestration. The
architect's prompt had to reason about service decomposition AND emit a
working docker-compose YAML inline. Infrastructure services
(`{database, cache, proxy, auth}`) appeared as placeholders in the
AI's compose with no real configuration — a CRM with FusionAuth would
discover that gap immediately.

Splitting the concerns lets:

- The **architect** stay small and prompt-focused. No file writes, no
  docker subprocess calls, no compose-string emitting.
- The **provisioner** own infrastructure templates as first-class code
  (postgres init.sql, FusionAuth kickstart.json with realm + roles +
  OAuth redirects, nginx upstreams when added later, etc.). Templates
  are deterministic Python; AI is reserved for planning decisions.
- **docker-compose.yml** is built from the structured plan, not parsed
  out of an AI string. Removes a class of LLM bugs (malformed YAML,
  wrong port mappings, wrong build paths).
- **Iteration story** becomes natural — the provisioner can be re-run
  on an existing project to apply a new milestone without rebuilding
  from scratch (foundation for the future Planner agent).

## Module layout

```
bizniz/architect/        — planning only
  architect.py      — decompose(), build() (thin orchestration)
  prompts/decompose_prompt.py
  prompts/schema.py      — no docker_compose field
  skeletons.py           — skeleton registry (seeding logic moved to provisioner)
  types.py               — ServiceDefinition, SystemArchitecture (docker_compose now optional)

bizniz/provisioner/      — materialization only
  provisioner.py         — Provisioner class, allocate_free_ports, cleanup
  compose_builder.py     — build_compose() — deterministic compose generation
  env_builder.py         — build_env_file() — groups env vars by prefix
  docker_builder.py      — build_image() — subprocess wrapper
  templates/
    base.py              — InfraTemplate ABC, registry, TemplateContext, TemplateOutput
    postgres.py          — PostgresTemplate (creates fusionauth DB by default)
    redis.py             — RedisTemplate
    fusionauth.py        — FusionAuthTemplate (kickstart.json + compose entry + .env)
    app_python.py        — PythonAppTemplate (generated Dockerfile + requirements.txt)
    app_typescript.py    — TypeScriptAppTemplate (generated Dockerfile + package.json)
  types.py               — ProvisionResult, ProvisionedService
  tests/
    test_postgres_template.py
    test_redis_template.py
    test_fusionauth_template.py
    test_compose_builder.py
    test_env_builder.py
    test_provisioner.py            (no Docker, no AI; uses build_images=False)
    functional/
      test_architect_provisioner_real.py   (calls real Gemini, marked functional)
```

## InfraTemplate API

Every template implements `render(ctx) -> TemplateOutput`:

```python
@dataclass
class TemplateContext:
    service: ServiceDefinition
    project_slug: str
    project_root: Path
    port_mappings: List[tuple]

@dataclass
class TemplateOutput:
    compose_service: Optional[dict]      # service entry under "services:"
    compose_volumes: List[str]            # top-level volume names
    compose_networks: List[str]           # top-level network names
    workspace_files: Dict[str, str]      # written under project_root/<workspace_name>/
    infra_files: Dict[str, str]          # written under infra/development/<workspace_name>/
    env_vars: Dict[str, str]             # merged into .env
    depends_on_services: List[str]       # names this template requires alongside
```

The provisioner aggregates all `compose_volumes`, `compose_networks`,
and `env_vars` across services, then uses `compose_builder.build_compose`
to assemble a single YAML file from the structured pieces.

## FusionAuth as default OAuth

When the architect's plan includes any "user account" / "login" concept,
the prompt instructs it to add:

- `service_type=auth`, `framework=fusionauth`, `port=9011`, `skeleton=none`
- A `postgres` service (FusionAuth requires it)

The provisioner then:

1. Renders `PostgresTemplate` — emits `postgres/init.sql` that creates a
   `fusionauth` database alongside the app DB, plus a healthcheck-aware
   compose entry. Volume `pgdata` persists data across runs.
2. Renders `FusionAuthTemplate` — emits
   `fusionauth/kickstart/kickstart.json` that pre-configures:
   - The default tenant's issuer URL
   - An application named after the project slug
   - Two roles: `admin` (super) and `user` (default)
   - OAuth redirect URLs for both React (5173) and Angular (4200) frontends
   - JWT settings (1h access, 30d refresh)
   - An initial admin user (email derived from project slug)
   - A bootstrap API key for backend services to call FusionAuth
3. Adds env vars consumed by the backend (`FUSIONAUTH_API_KEY`,
   `FUSIONAUTH_ISSUER`, `FUSIONAUTH_APPLICATION_ID`).

A new project boots with a fully-configured identity provider — no
manual UI clicks required.

## How an app service flows

Three paths depending on `service.skeleton`:

1. **`skeleton=fastapi|react|angular|teams-*`** → `seed_workspace()` from
   `bizniz/architect/skeletons.py` copies the skeleton tree into
   `project_root/<workspace_name>/` (skipping `.git`, `node_modules`,
   lockfiles), substitutes `{project_slug}` in package.json/pyproject
   names, and mirrors the skeleton's Dockerfile into
   `infra/development/<workspace_name>/Dockerfile` so compose finds it
   where it expects.
2. **`skeleton="none"` and `language="python"`** → `PythonAppTemplate`
   emits a generic FastAPI/Flask/Django Dockerfile + `requirements.txt`
   with framework defaults (fastapi → fastapi+uvicorn+pydantic+httpx).
3. **`skeleton="none"` and `language="typescript"`** → `TypeScriptAppTemplate`
   emits a Node 20 Dockerfile + `package.json` with jest + ts-jest +
   testing-library deps and a working jest config.

In all three cases `compose_builder` produces a per-service compose entry
with `build.context: ../../<workspace>` and
`dockerfile: ../../infra/development/<workspace>/Dockerfile`.

## Migration notes

If you were calling the old monolithic architect:

```python
architect = Architect(client=..., environment=..., workspace=...,
                         engineer_factory=..., project_parent="/parent")
architect.build(problem, project_name)
```

…it still works. `Architect.build()` constructs a default `Provisioner`
internally. To inject a custom one (e.g. `build_images=False` for tests):

```python
from bizniz.provisioner import Provisioner
prov = Provisioner(project_parent="/parent", build_images=False)
architect = Architect(..., provisioner=prov)
```

The `ArchitectSchema` and `SystemArchitecture` no longer require
`docker_compose` — keep this in mind if you were poking at architecture
JSON snapshots from prior runs. The field still exists as optional and
holds the AI's compose preview (used only in the human-readable
`docs/architecture.md`); the actual compose used to run the project is
the one the provisioner builds.
