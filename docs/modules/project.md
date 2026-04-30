# Project

`bizniz/project/`. The cross-service container.

## Purpose

A `Project` represents one bizniz-managed multi-service codebase — the thing the architect builds. It owns the project root directory, the `infra/development/` subtree (Docker configs), and a project-level database for cross-service tracking.

Layout produced by `Project.create_structure()` and the architect:

```
project_root/
├── .bizniz/
│   └── project.db                  # ProjectDB (only when bizniz_db is None)
├── backend/                        # one workspace per application service
│   ├── src/...
│   └── tests/...
├── frontend/
│   ├── src/...
│   └── tests/...
└── infra/
    └── development/
        ├── docker-compose.yml
        ├── .env
        ├── backend/
        │   └── Dockerfile          # mirrored from backend/Dockerfile if seeded from skeleton
        └── frontend/
            └── Dockerfile
```

## `Project` class (`project/project.py`)

| Constructor param | Notes |
|-------------------|-------|
| `root` | Path of the project root |
| `project_name` | Human-readable name (used as the project_id in the unified DB) |
| `bizniz_db` | Optional unified `BiznizDB` — when provided, `db` returns a `ProjectScope` instead of `ProjectDB` |

| Property / method | Notes |
|--------------------|-------|
| `root` | `Path` |
| `project_name` | `str` |
| `dev_root` | `root / infra / development` |
| `docker_root` | alias of `dev_root` |
| `db` | Lazy. `ProjectScope` if `bizniz_db` set, else `ProjectDB` (per-project SQLite at `.bizniz/project.db`) |
| `create_structure()` | Creates `dev_root` (and parents) |
| `get_docker_service_dir(service_name)` | `docker_root / service_name`, mkdirs |
| `get_service_workspace(service_name)` | Returns a `LocalWorkspace` at `root / service_name`, wired to `bizniz_db` (with project_id + service_name) |
| `write_docker_compose(content)` | Writes `dev_root / docker-compose.yml` |
| `write_env_file(content)` | Writes `dev_root / .env` |
| `get_issue_history()` / `get_architecture_changes()` / `get_service_status()` | DB shortcuts |
| `Project.from_name(project_name, parent="~", bizniz_db=None)` | Slugified construction |

## `ProjectDB` (`project/project_db.py`)

Standalone-mode SQLite database used when no unified `BiznizDB` is configured. Tracks:

- `services` — name, type, framework, language, workspace_path, image_name, status (`open`/`building`/`ready`/`failed`).
- `architecture_snapshots` — full `SystemArchitecture` JSON, versioned, with description.
- `issue_log` — cross-service issue ledger (which service / which issue title / status / strategy / iterations).
- `build_log` — `image_build` / `package_install` / `rebuild` events.
- `drift_events` — files coder produced that weren't in the architecture plan.

API includes `save_service`, `update_service_status`, `update_service_image`, `save_architecture_snapshot`, `log_issue`, `update_issue`, `log_build_event`, `log_drift_event`, plus their `get_*` counterparts. Schema details mirror the unified `BiznizDB.services` / `architecture_snapshots` / `issue_log` / `build_log` / `drift_events` tables.

## Example

```python
from bizniz.project.project import Project

project = Project.from_name("Pet Groomer", parent="/home/me/projects")
project.create_structure()

backend_ws = project.get_service_workspace("backend")
project.write_docker_compose("version: '3.9'\n...\n")

project.db.save_service(
    name="backend",
    service_type="backend",
    framework="fastapi",
    language="python",
    workspace_path=str(backend_ws.root),
)
```

## Interactions

- **Used by:** `Architect.build` for the entire project lifecycle.
- **Calls into:** `LocalWorkspace`, `BiznizDB.for_project(...)` (when unified), `ProjectDB` (when standalone).

## Gotchas

- **`db` returns different objects depending on `bizniz_db`.** `ProjectScope` and `ProjectDB` both expose the same business methods (`save_service`, `log_issue`, etc) so callers don't care, but type-narrowing IDEs may not see this.
- **`get_service_workspace` always uses the same naming convention.** `root / service_name`. If you change `service.workspace_name` mid-pipeline, the existing workspace stays orphan.
- **The architect handles `infra/development/` mirroring.** `Project` itself doesn't write per-service Dockerfiles — `Architect.build` does that (either by mirroring a skeleton's Dockerfile or by generating one).
- **`from_name` is the easiest entrypoint.** Direct `Project(root, project_name)` is also fine, but you have to slugify the parent path yourself.
