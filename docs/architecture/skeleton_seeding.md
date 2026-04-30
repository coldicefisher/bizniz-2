# Skeleton Seeding

The skeleton system is the way bizniz seeds new services from real, working repositories instead of asking the LLM to invent every line of boilerplate. It is brand-new (introduced in commit `5a95f97`) and lives in `bizniz/architect/skeletons.py`.

## Why skeletons exist

The pre-skeleton version of `Architect` had the LLM generate every Dockerfile, every dependency manifest, and every entry-point file from scratch. This produced a lot of broken auth flows, missing test infrastructure, inconsistent project layouts, and a slow startup for any service that needed batteries (database, sessions, JWT, etc).

A skeleton is a starter repo cloned to local disk that already has those batteries in place. The architect can pick a skeleton per service in its decomposition step; the seeding code copies the repo into the workspace and substitutes a couple of placeholders.

## Where skeletons live on disk

Skeletons live under `$BIZNIZ_SKELETONS_DIR` (default: `~/`). The function `skeletons_root()` returns this path, and `skeleton_source_path(skeleton_info)` resolves a specific skeleton by joining `skeletons_root() / skeleton.relative_path`.

| Skeleton key | Relative path under `$BIZNIZ_SKELETONS_DIR` | Framework / language | Default port |
|--------------|---------------------------------------------|----------------------|--------------|
| `fastapi` | `bizniz-skeleton-fastapi` | FastAPI / Python | 8000 |
| `react` | `bizniz-skeleton-react` | React+TS+Vite | 5173 |
| `angular` | `bizniz-skeleton-angular` | Angular / TypeScript | 4200 |
| `teams-backend` | `bizniz-skeleton-teams/backend` | FastAPI fan-out backend | 8000 |
| `teams-consumer` | `bizniz-skeleton-teams/consumer` | Celery worker (Python) | (none) |
| `teams-frontend` | `bizniz-skeleton-teams/frontend-angular` | Angular | 4200 |

Full details are in [reference/skeleton_reference.md](../reference/skeleton_reference.md).

## The registry API (`bizniz/architect/skeletons.py`)

| Function | Returns | Purpose |
|----------|---------|---------|
| `list_skeletons()` | `list[SkeletonInfo]` | Iterate everything in the registry |
| `get_skeleton(name)` | `Optional[SkeletonInfo]` | Lookup by name; returns `None` for `"none"` or unknown |
| `skeletons_root()` | `Path` | The base dir, configurable via `BIZNIZ_SKELETONS_DIR` |
| `skeleton_source_path(info)` | `Path` | Absolute path to the cloned skeleton |
| `seed_workspace(name, dest, project_slug, service_name)` | `list[str]` (relative paths) | Copy the skeleton, substitute placeholders, return file list |
| `skeletons_summary_for_prompt()` | `str` | Human-readable block injected into the architect prompt |

`SkeletonInfo` is a frozen dataclass with `name`, `relative_path`, `service_type`, `framework`, `language`, `container_port`, `description`.

## The end-to-end flow

The architect uses skeletons during the `build()` pipeline. Step numbers below match the comments in `architect.py`.

```
build(problem_statement, project_name)
│
├─ 1. decompose(...)
│    ↳ AI chooses skeleton: "fastapi" | "react" | "angular" |
│      "teams-backend" | "teams-consumer" | "teams-frontend" | "none"
│      per service — schema enforces this enum.
│
├─ 1b. _allocate_free_ports(architecture)
│    ↳ Walks every service.port, picks free host ports,
│      rewrites docker_compose YAML in place.
│
├─ 2.  Project(...).create_structure()    creates infra/development/
│
├─ 2b. _cleanup_existing_project(project_slug)
│    ↳ docker rm + docker rmi for prior builds of this slug,
│      sweeps orphan bizniz-pytest-* containers.
│
├─ 3.  for each application service:
│      ┌─ skeleton = get_skeleton(service.skeleton)
│      ├─ if skeleton is not None:
│      │     copied = seed_workspace(
│      │         skeleton_name=skeleton.name,
│      │         dest=workspace.root,
│      │         project_slug=architecture.project_slug,
│      │         service_name=service.name,
│      │     )
│      │     # mirror the skeleton's Dockerfile into infra/development/<svc>/
│      │     copy(workspace.root/Dockerfile, docker_dir/Dockerfile)
│      │
│      └─ else (or seeding raised FileNotFoundError):
│            generate boilerplate Dockerfile + requirements.txt / package.json
│
├─ 4.  write docker-compose.yml + .env to infra/development/
│
├─ 5.  for each application service:
│        docker build -t <slug>-<svc>:dev -f <dockerfile> <workspace.root>
│
└─ 6.  topo-sort services by depends_on, then per layer dispatch
       Engineer instances (in parallel within a layer).
```

## What `seed_workspace` does precisely

```python
def seed_workspace(skeleton_name, dest, project_slug, service_name) -> list[str]:
    skeleton = get_skeleton(skeleton_name)
    if skeleton is None:
        return []

    src = skeleton_source_path(skeleton)
    if not src.exists():
        raise FileNotFoundError(...)   # tells the user how to clone it

    # Recursively walk, skipping these directory/file names:
    _EXCLUDE_NAMES = {".git", ".github", "node_modules", "__pycache__",
                      ".pytest_cache", "dist", "build",
                      "package-lock.json", ".env"}

    # Copy text files with placeholder substitution:
    text = text.replace("{project_slug}", project_slug)
    text = text.replace("{service_name}", service_name)

    # Binary files are copied byte-for-byte.
    return list_of_relative_paths_copied
```

Two things to call out:

1. **`.env` is excluded but `.env.example` is not.** Skeletons should ship an `.env.example` so the AI/operator can fill it in.
2. **`package-lock.json` is excluded** so a fresh `npm install` runs in the seeded copy, picking up the host's resolver — this avoids platform-specific lockfile drift.

## What happens to the Dockerfile

After seeding, the Dockerfile lives at `<workspace_root>/Dockerfile` (where the skeleton put it). docker-compose looks for it under `infra/development/<service>/Dockerfile` (where the AI's compose file points), so `architect.py` mirrors the file:

```python
skeleton_dockerfile = Path(workspace.root) / "Dockerfile"
if skeleton_dockerfile.exists():
    (docker_dir / "Dockerfile").write_text(skeleton_dockerfile.read_text())
```

Everything else (requirements.txt, package.json, source files) stays inside the workspace.

## Fallback path

If `seed_workspace` raises `FileNotFoundError` (the skeleton isn't cloned on this machine), the architect logs the failure and falls through to the legacy "generate boilerplate" path:

```python
except FileNotFoundError as e:
    log(f"Architect: skeleton seeding failed for '{service.name}': {e}")
    log(f"Architect: falling back to generated boilerplate for '{service.name}'")
    skeleton = None  # fall through

if skeleton is None:
    dockerfile_content = self._generate_dockerfile(service)
    (docker_dir / "Dockerfile").write_text(dockerfile_content)
    if service.language == "python":
        workspace.write_file("requirements.txt", _generate_requirements_txt(service))
    elif service.language == "typescript":
        workspace.write_file("package.json", _generate_package_json(service, project_slug))
```

## How the AI knows which skeleton to pick

`Architect`'s prompt includes the result of `skeletons_summary_for_prompt()`, which lists every skeleton and its description. The schema (`ArchitectSchema`) requires `skeleton` to be one of the registered names or `"none"`, so the AI cannot invent unknown values.

Picking guidance baked into the descriptions:

- `react` is the default frontend.
- `angular` is for dashboard-heavy / data-dense UIs.
- `teams-*` are for Microsoft Teams-style realtime fan-out architectures (use the trio together).
- `fastapi` is the default backend.
- `none` is for infrastructure services (database/cache/proxy) and any service the AI can't match.

## Adding a new skeleton

1. Clone the skeleton repo to `~/bizniz-skeleton-<name>` (or any path under `$BIZNIZ_SKELETONS_DIR`).
2. Add an entry to the `_SKELETONS` dict in `bizniz/architect/skeletons.py` with `name`, `relative_path`, `service_type`, `framework`, `language`, `container_port`, and a one-paragraph description that tells the AI when to pick it.
3. Add the new name to the `skeleton` enum in `bizniz/architect/prompts/schema.py:ArchitectSchema`.
4. Make sure your skeleton uses `{project_slug}` and `{service_name}` placeholders in any file the substitution should reach (typically `pyproject.toml`, `package.json`, `README.md`, `docker-compose.override.yml`).

The architect picks it up on the next decomposition.

## Gotchas

- **Skeleton root is `Path.home()` by default.** If you put skeletons elsewhere, set `BIZNIZ_SKELETONS_DIR` before running bizniz.
- **The skeleton's Dockerfile is canonical.** The architect copies it into `infra/development/<service>/`. Don't expect the architect's `_generate_dockerfile` boilerplate to override it.
- **The teams-* trio shares one source tree.** All three live under `bizniz-skeleton-teams/{backend,consumer,frontend-angular}` so a single `git clone` covers them.
- **`schema.py` is the source of truth for valid skeleton names.** If you add to `_SKELETONS` but forget the schema enum, the LLM's structured output will be rejected.
