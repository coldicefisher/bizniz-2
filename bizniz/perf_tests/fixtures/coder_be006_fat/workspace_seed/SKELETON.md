# FastAPI Skeleton — Directory Contract

Read this **before** generating code. The skeleton's `app/main.py`,
schemas, models, and routes are already wired up. Your code goes
into the extension points below. **Never edit a file the skeleton
ships** — create new files instead.

## Authentication: FusionAuth

This skeleton delegates ALL authentication to **FusionAuth** (a
separate Docker service in the compose stack). The skeleton provides:

- **Auth routes** (`app/api/routes/auth.py`) — thin proxies to
  FusionAuth's API. Register, login, refresh, password reset, email
  verification all go through FusionAuth. Do NOT reimplement auth.
- **JWT validation** (`app/core/auth.py`) — validates FusionAuth-issued
  RS256 JWTs via JWKS. Provides `get_current_user` and `require_roles`
  as FastAPI dependencies.
- **User sync** — on every auth check, the local User table is upserted
  from FusionAuth's JWT claims. The local User exists for foreign key
  relationships only — FusionAuth is the source of truth for identity.

**To protect an endpoint:**
```python
from app.core.auth import get_current_user, require_roles
from app.models.user import User

@router.get("/my-stuff")
async def my_stuff(user: User = Depends(get_current_user)):
    # user.user_id matches the FusionAuth user ID
    ...

@router.get("/admin-only", dependencies=[Depends(require_roles("admin"))])
async def admin_only():
    ...
```

**Do NOT:**
- Implement your own password hashing or JWT creation
- Store passwords in the local database
- Create your own login/register endpoints
- Bypass FusionAuth for any auth operation

## The five extension points

| You add | Skeleton already ships | Auto-discovery |
|---|---|---|
| `app/api/routes/<feature>.py` | `app/api/routes/auth.py`, `app/api/routes/docs.py` | yes — `app/main.py` mounts every router under `/api/v1` |
| `app/models/<feature>.py` | `app/models/user.py` | yes — `app/models/__init__.py` imports every module so SQLAlchemy registers tables |
| `app/schemas/<feature>.py` | `app/schemas/auth.py`, `app/schemas/docs.py` | no — import explicitly where used |
| `app/services/<feature>.py` | `app/services/docs_loader.py` | no — business-logic helpers |
| `tests/unit/test_<feature>.py` | tests for skeleton modules | no — pytest discovery handles it |

## Docs serving (always present — never edit)

Every generated app ships with `/api/v1/docs/*` routes that serve
the markdown HumanDocsGenerator writes to `<project>/docs/`. Files:

- `app/api/routes/docs.py` — 3 endpoints (index, article, search), all gated by `get_current_user`.
- `app/schemas/docs.py` — DTOs (`DocsIndexDto`, `DocsArticleDto`, `DocsSearchHitDto`).
- `app/services/docs_loader.py` — in-memory loader with mtime-based reload + path-traversal guard.

The Provisioner mounts `<project>/docs/` into the container at `/app/docs:ro`.
The viewer in the React/Angular skeleton consumes these routes;
there is no direct filesystem read from the frontend.

**Do not edit these files.** If you need a docs-related capability
(e.g. "admin-only docs section"), add a NEW route file that imports
`get_docs_loader` and `get_current_user`.

## Adding new endpoints (example: `/properties`)

1. **Schemas** — create `app/schemas/properties.py`:
   ```python
   from pydantic import BaseModel
   class PropertyRead(BaseModel):
       id: int
       address: str
       unit_count: int
   ```

2. **Models** — create `app/models/property.py`:
   ```python
   from sqlalchemy import ForeignKey
   from sqlalchemy.orm import Mapped, mapped_column
   from sqlalchemy.dialects.postgresql import UUID
   from app.db.base import Base
   class Property(Base):
       __tablename__ = "property"
       id: Mapped[int] = mapped_column(primary_key=True)
       owner_id: Mapped[uuid.UUID] = mapped_column(
           UUID(as_uuid=True), ForeignKey("user.user_id"), nullable=False
       )
       # user.user_id links to the FusionAuth-synced local User
   ```

3. **Router** — create `app/api/routes/properties.py`:
   ```python
   from fastapi import APIRouter, Depends
   from app.core.auth import get_current_user, require_roles
   from app.models.user import User
   router = APIRouter(prefix="/properties", tags=["properties"])

   @router.get("")
   async def list_properties(user: User = Depends(require_roles("landlord"))):
       ...
   ```

   Auto-mounted at `/api/v1/properties` — no edit to `app/main.py`.

4. **Tests** — pick the right fixture for the right job:

   | Fixture | Backend | Use for |
   |---|---|---|
   | `client` | ASGI + sqlite-memory | Pure-Python helpers, schema validation, ORM serialization. **NOT for route tests that need real DB semantics.** |
   | `live_client` | ASGI + **real Postgres** | **Any route test that touches the DB.** Transactional rollback per test — never drops tables. |
   | `db_session` | sqlite-memory | Direct ORM tests that don't need the app. |
   | `live_postgres_session` | real Postgres | Direct ORM tests that need real Postgres types/extensions. |

   **HARD CONTRACTS** (violations should be caught in code review):

   - Never call `Base.metadata.drop_all()` against a live database.
     The 2026-05-16 crm_v1 incident shipped a broken app because a
     per-test `create_all` + `drop_all` pattern dropped the
     production tables at the end of the suite, and the next
     process boot's lifespan failed to recreate them. Use the
     transactional rollback in `live_postgres_session` instead.
   - Route tests that hit DB-touching endpoints must use
     `live_client`, not `client`. sqlite-memory hides Postgres-
     specific failures (missing tables, dialect quirks, extension
     absence). If your test calls a route handler and the route
     queries a model, use `live_client`.
   - Tests of pure helpers (no app, no DB, just a function) need
     no fixture at all — keep them sqlite-free.

## What you may NOT do

- Do not edit `app/main.py`. Auto-discovery handles routing.
- Do not edit `app/models/user.py`, `app/schemas/auth.py`, or
  `app/api/routes/auth.py`. Those are the skeleton's auth surface
  and downstream code imports specific symbols from them.
- Do not edit `app/db/base.py`, `app/db/session.py`, `app/core/auth.py`,
  `app/core/config.py`. The skeleton's wiring.
- Do not edit `app/models/__init__.py` or `app/schemas/__init__.py` —
  auto-discovery already handles new files.
- Do not create a top-level package outside `app/` (e.g. `myproject/`).
  The Dockerfile only copies `app/` and runs `uvicorn app.main:app`.
  Anything outside `app/` is dead code in the running container.
- Do not implement your own auth — use `get_current_user` and
  `require_roles` from `app/core/auth`.

## The contract in one sentence

Drop new files into `app/api/routes/<feature>.py`,
`app/models/<feature>.py`, `app/schemas/<feature>.py`. Never edit
the files the skeleton already ships. Auth goes through FusionAuth.
