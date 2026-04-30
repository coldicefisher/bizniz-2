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
| `ai_client_factory` | `Optional[Callable[[model_name], BaseAIClient]]` | Required only when an AI escape hatch is enabled. Factory pattern keeps the Provisioner from importing `BiznizConfig` directly. |
| `ai_fallback_enabled` | `bool = False` | Opt-in: AI fills in templates for unknown infrastructure frameworks. See [AI escape hatches](#ai-escape-hatches). |
| `ai_fallback_model` | `str = "gemini-flash"` | Model name passed to `ai_client_factory` for fallback calls. |
| `ai_recovery_enabled` | `bool = False` | Opt-in: AI patches a failed Dockerfile and re-attempts the build. |
| `ai_recovery_model` | `str = "gemini-pro"` | Model for recovery calls — uses a top-tier model since the failure may be subtle. |
| `ai_recovery_max_retries` | `int = 2` | Hard cap on per-service AI rebuild attempts. |
| `ai_template_cache_dir` | `Optional[Path]` | Override the AI fallback cache location. Defaults to `BIZNIZ_TEMPLATE_CACHE_DIR` env var or `~/.bizniz/template_cache`. |

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

## AI escape hatches

Two opt-in AI calls let the Provisioner handle cases the static templates and skeletons can't: unknown infrastructure frameworks, and Docker builds that fail on a real machine. Both default to **off**. Both have **structural guarantees** — the AI's response schema explicitly excludes anything that would invalidate the architect's plan (ports, depends_on, networks, compose-level wiring).

### Hatch 1: Template-gap fallback

**When it fires.** The architect requested an infrastructure service whose `framework` has no template registered (e.g. `clickhouse`, `kafka`, `dgraph`). The static path returns `template_name=None` and the compose entry is dropped. With `ai_fallback_enabled=True` and an `ai_client_factory`, the Provisioner asks an AI (default `gemini-flash`) to produce a starter setup.

**What the AI emits** (`AIFallbackResponse`):

- `dockerfile`: full Dockerfile content, OR
- `upstream_image`: a published image like `clickhouse/clickhouse-server:24.3` (preferred for well-known infra)
- `env_vars`: dict of env vars
- `infra_files`: dict of additional config files (paths relative to the workspace dir)
- `notes`: brief explanation

**What the AI does NOT emit.** The schema has no fields for ports, depends_on, networks, healthcheck, or volumes. The Provisioner layers those on from the architect's plan when wrapping the response in an `AIFallbackTemplate`.

**Caching.** Responses are cached at `~/.bizniz/template_cache/<framework>__<service_type>.json` (override with `BIZNIZ_TEMPLATE_CACHE_DIR` or constructor arg). Subsequent runs against the same `(framework, service_type)` pair are zero-AI. There's no TTL — invalidate by deleting the cache file.

**Failure mode.** If the AI call raises (rate limit, schema-validation error, empty response), the Provisioner logs a warning and falls through to the original behavior: no compose entry for that service. Static templates with the same framework name always win — fallback only triggers on a registry miss.

### Hatch 2: Build-failure recovery

**When it fires.** A `docker build` raises during `_build_images_per_action`. With `ai_recovery_enabled=True`, the Provisioner reads the current Dockerfile, sends it plus the build error tail (last 4KB of stderr) to an AI (default `gemini-pro`), writes the patched Dockerfile back, and retries the build. Up to `ai_recovery_max_retries` (default 2) attempts.

**What the AI emits** (`AIRecoveryResponse`):

- `dockerfile`: full patched Dockerfile (not a diff)
- `explanation`: brief reason for the patch

**What the AI does NOT emit.** The schema rejects any compose-level changes. The AI cannot edit ports, depends_on, requirements.txt, or anything outside the Dockerfile.

**Backups.** Before each rewrite, the prior Dockerfile is saved to `Dockerfile.pre-ai-recovery-<attempt>` next to the original so the user can audit what changed.

**Failure mode.** If retries exhaust, the AI call raises, or the AI returns an empty Dockerfile, the service is marked `failed` in the project DB exactly as it would be without the hatch. The recovery never widens its blast radius.

### Why opt-in

The escape hatches exist for the long tail of frameworks the registry doesn't cover and for the genuinely surprising build failures. They're off by default because:

- **Predictability.** Most users want `provision()` to be a pure function of the architecture. AI introduces non-determinism even with caching.
- **Cost.** Recovery in particular calls a top-tier model on a hot path (build failure mid-pipeline).
- **Audit trail.** When the AI rewrites a Dockerfile, that change isn't in git. Users opting in should be aware that `Dockerfile.pre-ai-recovery-*` files appear next to their Dockerfiles.

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
