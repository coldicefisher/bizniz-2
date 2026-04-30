# AutoArchitect

The system decomposer. `bizniz/architect/auto_architect.py` defines the agent at the very top of the pipeline: it takes a free-form problem statement plus a project name and returns a fully provisioned, Docker-built, multi-service workspace.

## Purpose

A single `AutoArchitect.build(...)` call performs every step needed to go from "I want a pet groomer scheduling app" to a directory tree containing one Dockerfile per service, a working `docker-compose.yml`, freshly built Docker images, and N `AutoEngineer` subprocesses fanning out to write actual code. It also owns the new skeleton-seeding flow described in [architecture/skeleton_seeding.md](../architecture/skeleton_seeding.md).

## Constructor

| Parameter | Type | Notes |
|-----------|------|-------|
| `client` | `BaseAIClient` | AI provider for the decomposition call |
| `environment` | `BaseExecutionEnvironment` | Required by `BaseAIAgent`; not actually invoked here |
| `workspace` | `BaseWorkspace` | The architect's own scratch workspace (used as a fallback parent dir) |
| `engineer_factory` | `Callable(workspace, on_status_message, image_name, language) → ContextManager[AutoEngineer]` | Used to spin up a fresh engineer per service |
| `project_parent` | `Optional[str]` | Where the project root is created. Defaults to `workspace.root.parent` |
| `max_retries` | `int = 3` | Number of attempts for the AI decomposition call |
| `on_event`, `on_status_message` | callbacks | Standard agent callbacks |

## Public API

### `decompose(problem_statement, project_name) → SystemArchitecture`

Calls the AI once with `AutoArchitectSchema` and returns the parsed `SystemArchitecture`. Used standalone if you only want the design (no Docker).

### `build(problem_statement, project_name, parallel=True, max_workers=4, layered=True) → ArchitectResult`

The full pipeline. Steps:

1. **Decompose.** Call `decompose(...)`.
2. **Port allocation.** `_allocate_free_ports(architecture)` picks free host ports for each service, rewriting the `docker_compose` YAML in place.
3. **Project structure.** Creates `infra/development/` under the project root.
4. **Cleanup.** `_cleanup_existing_project(project_slug)` removes containers + images from prior runs of the same slug, plus orphan `bizniz-pytest-*` containers.
5. **Snapshot.** Writes the `SystemArchitecture` JSON to the project DB and a human-readable `docs/architecture.md`.
6. **Per-service workspace + Docker config.** For every application service (`backend`, `frontend`, `worker`):
   - Create `LocalWorkspace` at `project_root/<workspace_name>/`.
   - If `service.skeleton` matches a registered skeleton, call `seed_workspace(...)` and mirror the skeleton's Dockerfile into `infra/development/<svc>/`. (See [architecture/skeleton_seeding.md](../architecture/skeleton_seeding.md).)
   - Otherwise, generate boilerplate Dockerfile + `requirements.txt` / `package.json`.
   - Register the service in the project DB.
7. **Compose + env.** Write `docker-compose.yml` and `.env`.
8. **Image build.** `docker build -t <slug>-<svc>:dev` per service.
9. **Engineer dispatch.** Topologically sort services by `depends_on`, then for each layer either run engineers sequentially or in parallel via a `ThreadPoolExecutor`.

The return value is `ArchitectResult` with `service_results: List[ServiceResult]`, the path to the generated docker-compose, and the project root.

## Engineering dispatch helpers

| Method | What it does |
|--------|--------------|
| `_dispatch_engineers_sequential(...)` | One service at a time |
| `_dispatch_engineers_parallel(...)` | Submits each service to a `ThreadPoolExecutor` of `max_workers` threads. Holds a `project_db_lock` because the SQLite project DB is shared. |
| `_dispatch_engineer(workspace, service, service_prompt, project, project_db_lock=None, layered=True)` | Creates an engineer via `engineer_factory(...)` and runs `engineer.run_layered(...)` (or per-issue dispatch when `layered=False`). Returns a `ServiceResult`. |

## Boilerplate generators

These are static methods used when no skeleton is selected:

| Method | Output |
|--------|--------|
| `_generate_dockerfile(service)` | Python: `python:3.12-slim` + `pip install -r requirements.txt` + `uvicorn` CMD. TypeScript: `node:20-slim` + `npm install` + `npx jest` (dev image; source bind-mounted at runtime). |
| `_generate_requirements_txt(service)` | Combines `service.requirements`, framework defaults (`fastapi`, `flask`, `django`), and always includes `pytest`. |
| `_generate_package_json(service, project_slug)` | Minimal `package.json` with `ts-jest` config; adds React testing deps if `service_type == "frontend"`. |
| `_generate_env_file(architecture)` | Writes `POSTGRES_*` / `REDIS_URL` defaults based on services in the architecture. |

## Module-level helpers

| Function | Purpose |
|----------|---------|
| `_is_host_port_free(port)` | Tries to `bind` (only succeeds if port is free) |
| `_find_free_port(preferred, taken)` | Walks up from `preferred` skipping `taken` and bound ports |
| `_allocate_free_ports(architecture)` | Rebinds collisions; rewrites compose YAML |
| `_cleanup_existing_project(slug, log)` | docker rm/rmi for `<slug>-*` images & their containers |
| `_sort_services_by_dependency(services)` | Topo-sort into wavefronts (services in same layer can run in parallel) |
| `_save_architecture_docs(project_root, architecture)` | Writes `docs/architecture.md` |

## Example

```python
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.architect.auto_architect import AutoArchitect
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.workspace.temp_workspace import TempWorkspace

cfg = BiznizConfig.find_and_load()

def make_engineer(workspace, on_status_message, image_name, language):
    # build_engineer returns a ContextManager[AutoEngineer]
    return build_engineer(cfg, workspace, image_name=image_name, language=language)

with TempWorkspace() as scratch:
    architect = AutoArchitect(
        client=cfg.make_client(cfg.architect_model),
        environment=PythonSandboxExecutionEnvironment(),
        workspace=scratch,
        engineer_factory=make_engineer,
        project_parent="/home/me/projects",
        on_status_message=print,
    )
    result = architect.build(
        problem_statement="A pet grooming scheduler with FastAPI backend + React frontend",
        project_name="Pet Groomer",
        layered=True,
    )
    print(result.project_root, result.docker_compose_path)
```

## Interactions

- **Calls into:** `BaseAIClient.get_text` (decomposition), `bizniz.architect.skeletons.{get_skeleton, seed_workspace}`, `Project.create_structure / get_service_workspace / get_docker_service_dir / write_docker_compose / write_env_file`, `subprocess.run("docker", ...)`, the engineer factory.
- **Called by:** the application entrypoint (CLI / script) at the top of the pipeline.

## Gotchas

- **The architect doesn't write engineering issues.** It only writes services + the architecture snapshot. The `AutoEngineer` it dispatches handles per-service issues (and stores them in the workspace DB, not the project DB).
- **Port reallocation mutates `architecture.docker_compose`.** If the AI's compose YAML was malformed, `_allocate_free_ports` updates `service.port` objects but logs the mismatch — the compose text isn't rewritten.
- **`_cleanup_existing_project` is destructive on rerun.** Anything tagged `<slug>-*:*` gets nuked. If you have a manually-tagged image colliding with the slug pattern, it's gone.
- **Skeleton failures fall back silently.** If the skeleton dir doesn't exist, the architect logs and falls through to boilerplate generation. Watch the status messages to confirm seeding actually happened.
- **`engineer_factory` MUST be a context manager factory.** `_dispatch_engineer` uses `with self._engineer_factory(...) as engineer:` so the factory return must implement `__enter__`/`__exit__` (and close DB connections on exit).
- **Parallel dispatch is per-layer.** Services within the same dependency layer run concurrently; layers are sequential.
