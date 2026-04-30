# Provisioner

The deterministic materializer. `bizniz/provisioner/provisioner.py` takes the `SystemArchitecture` produced by the [Architect](architect.md) and turns it into a real project on disk: directory tree, skeleton-seeded workspaces, infrastructure templates (postgres, FusionAuth, redis), `docker-compose.yml`, `.env`, and built Docker images.

It contains **no AI calls**. Every output is a pure function of the architecture plus the observed state of the project on disk and in Docker. That's by design — it keeps the Architect's plan from drifting as it gets materialized.

## Purpose

Architect plans. Provisioner materializes. The two roles split for two reasons:

1. **Determinism.** Materialization shouldn't be subject to the AI's whim. Once the architecture is fixed, every subsequent build of it produces the same files in the same places.
2. **Idempotency.** The Provisioner can be run repeatedly against the same project (every milestone in `evolve` mode, every retry of a failed run) without clobbering user / engineer-generated code.

The pipeline is **probe → reconcile → materialize**: read what's already on disk + in the DB + in Docker, compare against the desired architecture, and execute exactly the per-service actions needed to converge.

## Constructor

| Parameter | Type | Notes |
|-----------|------|-------|
| `project_parent` | `str \| Path` | Parent dir under which `<project_slug>/` is created |
| `on_status_message` | `Optional[Callable[[str], None]]` | Log callback. Receives every status line the Provisioner emits (probe results, reconcile decisions, image build progress, skeleton clone status). |
| `build_images` | `bool = True` | When `False`, skip `docker build` (used by tests) |

## Public API

### `provision(architecture, project_name, prune=False) → ProvisionResult`

The main entry point. Always probes first, then reconciles, then materializes.

Steps:

1. **Project root.** Ensures `<project_parent>/<project_slug>/infra/development/` exists.
2. **Probe.** `probe(project_slug, project.root)` reads DB + filesystem + Docker → `ProvisionState`.
3. **Reconcile.** `_reconcile(architecture, state)` produces a `ReconcileAction` per service: `create`, `update`, or `preserve` (plus a `rebuild_image` flag).
4. **Port allocation.** Free-ports only re-mapped for services flagged `create`. Preserved services keep their host ports.
5. **DB snapshot.** Writes the new `SystemArchitecture` JSON to `architecture_snapshots`. Prior snapshots are kept as history.
6. **Per-service materialization.** Dispatches each service to `_provision_service` (`create`) or `_evolve_service` (`update`/`preserve`).
7. **Compose + .env.** Always regenerated from the full architecture.
8. **Image build.** Per `rebuild_image`: builds new images, rebuilds extended ones, leaves preserved-with-image services alone (unless their image is missing from Docker, in which case reconcile flagged a rebuild).
9. **Optional prune.** When `prune=True`, removes orphan Docker images for services that exist in `state.project_images` but not in the desired architecture (e.g. removed during a refactor).

### `evolve(architecture, project_name) → ProvisionResult`

Thin alias for `provision(prune=False)`. Kept for readability at call sites that want to signal "this is a milestone evolve, not a fresh build". Functionally identical otherwise.

### `probe(project_slug, project_root=None) → ProvisionState`

Pure observation. No side effects. Returns:

| Field | Notes |
|-------|-------|
| `project_root_exists` | False if `<parent>/<slug>` doesn't exist |
| `compose_exists` / `env_exists` | True iff `infra/development/{docker-compose.yml, .env}` are present |
| `last_architecture_snapshot_json` | Most recent row in `architecture_snapshots`, or `None` |
| `services` | One `ProbedService` per row in the project DB's `services` table |
| `orphan_workspace_dirs` | Directory names under `project_root/` that aren't tracked in the DB and aren't `infra` / `.bizniz` |
| `project_images` | All Docker images matching `<slug>-*:*` (from `docker images --filter reference=...`) |

`ProbedService` carries:

- `db_recorded`, `db_workspace_path`, `db_status`, `db_image_name` — what the DB says
- `workspace_exists_on_disk` — is `project_root/<name>/` a directory?
- `has_dockerfile` — does `infra/development/<name>/Dockerfile` exist?
- `image_in_docker` — is `<slug>-<name>:dev` present in the Docker image list?

### Reconcile decisions

`_reconcile(architecture, state)` walks the architecture and emits one `ReconcileAction` per service:

| Observed state | Architect's `evolve_state` | Action | `rebuild_image` |
|----------------|---------------------------|--------|-----------------|
| Not in DB | any | `create` | yes |
| In DB, app workspace missing on disk | any | `create` | yes |
| Present, healthy | `new` | `create` | yes |
| Present, healthy | `extended` | `update` | yes |
| Present, healthy | `unchanged` | `preserve` | no |
| Present, image missing from Docker | `unchanged` | `preserve` | **yes** (recover the missing image) |

Infrastructure services (`database`, `cache`, `proxy`, `auth`) are not subject to the workspace-missing drift check — they don't have application workspaces.

## Result type

`ProvisionResult`:

| Field | Type | Notes |
|-------|------|-------|
| `project_name` | `str` | |
| `project_slug` | `str` | |
| `project_root` | `str` | Absolute path |
| `compose_path` | `str` | `<project_root>/infra/development/docker-compose.yml` |
| `env_path` | `str` | `<project_root>/infra/development/.env` |
| `services` | `List[ProvisionedService]` | One entry per architecture service, with `image_name` / `image_built` set if a build ran |
| `port_remap` | `Dict[str, tuple]` | `service_name → (old_port, new_port)` for any port that had to be re-allocated |

## Template registry

Infrastructure services are materialized via the template registry in `bizniz/provisioner/templates/`. See `bizniz/provisioner/templates/registry.py` for the registered set. Currently:

- `postgres` — initdb scripts + per-service database injection (e.g. fusionauth gets its own DB)
- `fusionauth` — kickstart.json, env vars, depends-on wiring
- `redis` — minimal compose entry, default config

App services (`backend`, `frontend`, `worker`) are materialized either by **skeleton seeding** (when `service.skeleton ∈ {fastapi, react, angular, teams-*}`) or by the generic `__python_app__` / `__typescript_app__` templates.

When a skeleton's repo isn't present locally, `seed_workspace()` auto-clones it from `github.com/coldicefisher/<repo>.git` into `BIZNIZ_SKELETONS_DIR` (default `~/`). Failures (missing git, network error, malformed repo) raise `FileNotFoundError` and the Provisioner falls back to the generic app template.

## Idempotency rules

- **Workspaces are never re-seeded.** If reconcile says `update` or `preserve` for an app service, the user's / engineer's code is left untouched. Only `create` triggers a fresh skeleton copy.
- **Templates re-render every time.** They're pure functions of the architecture; re-rendering produces identical output. Safe to overwrite.
- **`docker-compose.yml` and `.env` are always regenerated.** Don't hand-edit them — the next provision call will overwrite.
- **Ports stick.** Once a service's port is allocated, subsequent runs preserve it (reconcile never re-maps preserved services). Only `create` services go through free-port allocation.
- **Images are kept by default.** Removing a service from the architecture leaves its old image in Docker — pass `prune=True` to clean those up.

## Interactions

- **Called by:** the [Architect](architect.md) at the top of the pipeline (`Architect.build` → Provisioner.provision) and the milestone-walk loop in `architecture/evolve_mode.md` (`Architect.evolve` produces a new architecture, then `Provisioner.evolve` materializes it).
- **Calls into:** the project DB (`ProjectDB.save_service`, `save_architecture_snapshot`, `update_service_image`, `log_build_event`), `bizniz.architect.skeletons.seed_workspace`, the template registry, `subprocess.run("docker", ...)` for image builds and image inventory.

## Example

```python
from bizniz.architect.types import SystemArchitecture
from bizniz.provisioner import Provisioner

provisioner = Provisioner(
    project_parent="/home/me/projects",
    on_status_message=print,
)

# First build — everything is "create"
result = provisioner.provision(architecture, project_name="Pet Groomer")

# Probe state directly
state = provisioner.probe("pet_groomer")
for svc in state.services:
    print(svc.name, svc.workspace_exists_on_disk, svc.image_in_docker)

# Re-provision after a milestone — preserve existing services, add new ones
provisioner.evolve(updated_architecture, project_name="Pet Groomer")

# Aggressive cleanup of services no longer in the architecture
provisioner.provision(updated_architecture, project_name="Pet Groomer", prune=True)
```

## Gotchas

- **Pruning is destructive.** `prune=True` runs `docker rmi -f` on every `<slug>-*:*` image whose service name isn't in the desired architecture. If you renamed a service (vs added/removed), the old name's image will be pruned even though the workspace still exists.
- **Probe trusts the DB row.** If a service appears in the DB but its workspace was deleted by hand, reconcile flags `create` and the workspace will be re-seeded from skeleton — losing whatever the engineer had written there. The DB is the source of truth; don't `rm -rf` workspaces without also clearing the DB row.
- **`docker images` filter is name-based.** Two unrelated projects with overlapping slug prefixes (`crm` and `crm_v2`) will see each other's images via `--filter reference=crm-*`. Avoid colliding slugs.
- **Image-missing-but-otherwise-unchanged → forced rebuild.** If you nuked the Docker images by hand (`docker system prune`) but left the project intact, the next provision call will rebuild everything regardless of `evolve_state` — reconcile detects the gap and flips `rebuild_image=True` on preserved services.
