# Skeleton Reference

Every skeleton registered in `bizniz/architect/skeletons.py:_SKELETONS`.

For the bigger-picture explanation of how skeletons are seeded, see [architecture/skeleton_seeding.md](../architecture/skeleton_seeding.md).

## Source layout

Skeletons live under `$BIZNIZ_SKELETONS_DIR` (defaults to the user's home directory). Set the env var to point elsewhere if your skeletons aren't under `~`.

```
$BIZNIZ_SKELETONS_DIR/
├── bizniz-skeleton-fastapi/
├── bizniz-skeleton-react/
├── bizniz-skeleton-angular/
└── bizniz-skeleton-teams/
    ├── backend/
    ├── consumer/
    └── frontend-angular/
```

## Registry

| Name | Relative path | Service type | Framework | Language | Container port |
|------|---------------|--------------|-----------|----------|-----------------|
| `fastapi` | `bizniz-skeleton-fastapi` | backend | fastapi | python | 8000 |
| `react` | `bizniz-skeleton-react` | frontend | react | typescript | 5173 |
| `angular` | `bizniz-skeleton-angular` | frontend | angular | typescript | 4200 |
| `teams-backend` | `bizniz-skeleton-teams/backend` | backend | fastapi | python | 8000 |
| `teams-consumer` | `bizniz-skeleton-teams/consumer` | worker | celery | python | (none) |
| `teams-frontend` | `bizniz-skeleton-teams/frontend-angular` | frontend | angular | typescript | 4200 |

## Per-skeleton notes

### `fastapi`

Production-leaning FastAPI starter. Includes:

- Login / refresh / email verification / password reset / role-checking auth.
- Docker + docker-compose-friendly Dockerfile.
- pytest unit and functional test scaffolding.
- Structured logging.

Pick this for any general backend service.

### `react`

React + TypeScript + Vite frontend with:

- Auth flow (signup / login).
- Routing.
- Jest test setup.
- Docker.

Default frontend for general SPAs.

### `angular`

Angular frontend with:

- Material Design components.
- NgRx state management.
- Theming.
- Jasmine / Karma tests.
- Docker.

Use for dashboard-heavy or data-dense UIs where Angular's structure pays off.

### `teams-backend`

Realtime fan-out feed backend (FastAPI + producer). Use as one of three pieces in a Microsoft-Teams-style architecture (paired with `teams-consumer` and `teams-frontend`).

### `teams-consumer`

Realtime fan-out feed consumer (Celery worker). No HTTP port. Use alongside `teams-backend`.

### `teams-frontend`

Angular frontend wired for realtime fan-out feeds. Pair with `teams-backend` + `teams-consumer`.

### `none`

Sentinel value the AI emits for services where no skeleton applies — typically infrastructure containers (`postgres`, `redis`, `nginx`) or anything the architect needs to generate boilerplate for.

## Placeholder substitution

When `seed_workspace(...)` copies a skeleton, every text file is run through:

```
content.replace("{project_slug}", project_slug).replace("{service_name}", service_name)
```

So in your skeleton, embed the slug / service name in:

- `pyproject.toml` (`name = "{project_slug}"`)
- `package.json` (`"name": "{project_slug}-{service_name}"`)
- `README.md`
- `docker-compose.override.yml` if you ship one
- Any internal config that references the project name

## Excluded paths

These names are skipped during copy (`bizniz/architect/skeletons.py:_EXCLUDE_NAMES`):

- `.git`, `.github`, `.pytest_cache`, `__pycache__`
- `node_modules`, `dist`, `build`
- `package-lock.json`
- `.env` (ship `.env.example` instead)

## Adding a new skeleton

1. Clone or build the starter repo at `$BIZNIZ_SKELETONS_DIR/<dir-name>`.
2. Add a `SkeletonInfo(...)` entry to `_SKELETONS` in `bizniz/architect/skeletons.py`.
3. Add the new name to the `skeleton` enum in `bizniz/architect/prompts/schema.py:ArchitectSchema`.
4. Use `{project_slug}` and `{service_name}` placeholders wherever the skeleton needs project-specific values.
5. Bump no version, no migration — the architect picks it up immediately.

## Cloning the existing skeletons

The skeleton-not-found error message includes a `git clone` hint based on the GitHub user `coldicefisher`:

```
git clone https://github.com/coldicefisher/<dir-name>.git $BIZNIZ_SKELETONS_DIR/<dir-name>
```

If your fork lives elsewhere, edit the error message in `seed_workspace(...)` or just clone manually to the expected path.
