import importlib
import logging
import pkgutil
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.db.base import Base
from app.db.migrations import recipes as recipes_migration
from app.db.session import engine
import app.api.routes as _routes_pkg
import app.models  # noqa: F401 — triggers auto-import of every model module

settings = get_settings()

# Standard error envelope (BE-006-fix1)
# -------------------------------------
# All non-2xx responses returned by the FastAPI app go through three
# handlers below that wrap the response body in the spec-mandated shape
# ``{"error": <machine_code>, "message": <human readable>,
# "field_errors": {<field>: <msg>, ...}?}``.
#
# Why machine + human + optional field_errors:
#   * ``error`` is the stable, machine-readable code clients branch on
#     (e.g. ``recipe_not_found`` → SPA shows "no such recipe" UI).
#   * ``message`` is a fallback display string. Clients SHOULD prefer
#     their own i18n keyed off ``error`` and only fall back to
#     ``message`` for codes they don't yet handle.
#   * ``field_errors`` is included only on validation 400s; the keys are
#     bare field names (``title``, ``servings``) so the SPA can attach
#     them to the right form input.
#
# Why we register HTTPException + RequestValidationError handlers:
#   * Without the HTTPException handler, raises like
#     ``HTTPException(404, detail='recipe_not_found')`` surface as the
#     FastAPI default ``{"detail": "recipe_not_found"}`` — which
#     violates the spec envelope.
#   * Without the RequestValidationError handler, Pydantic body failures
#     return a 422 with FastAPI's default ``{"detail": [...]}`` list —
#     the spec explicitly says these must be remapped to 400 with the
#     standard envelope.
#   * Without the OperationalError handler, a DB-readonly window or a
#     dropped Postgres connection surfaces as an opaque 500 with no
#     envelope — operators can't distinguish "transient infra" from
#     "real bug" and the SPA can't auto-retry.

_HTTP_ERROR_MESSAGE_MAP: dict[str, str] = {
    "recipe_not_found": "Recipe not found.",
    "invalid_recipe_id": "Recipe id is not a valid UUID.",
    "unauthenticated": "Authentication required.",
    "invalid_token": "Authentication token is invalid.",
    "token_expired": "Authentication token has expired.",
    "no_role_assigned": "User has no role assigned.",
    "forbidden": "Access denied.",
    "auth_service_unavailable": "Authentication service is unavailable.",
    "duplicate_email_in_mirror": "Internal mirror conflict.",
}


def _humanize_for_code(code: str) -> str:
    """Return a human-readable message for a known machine code, else the code itself."""
    return _HTTP_ERROR_MESSAGE_MAP.get(code, code)


def _field_name_from_loc(loc: tuple[Any, ...]) -> str:
    """Extract a flat field name from a pydantic ``loc`` tuple.

    Pydantic loc tuples are prefixed with the scope (``'body'``,
    ``'query'``, ``'path'``) followed by the field path. Strip the
    scope so the SPA only sees the actual field name (e.g.
    ``loc=('body', 'title')`` → ``'title'``; nested
    ``loc=('body', 'ingredients', 2)`` → ``'ingredients.2'``).
    """
    parts = list(loc)
    if parts and parts[0] in ("body", "query", "path", "header", "cookie"):
        parts = parts[1:]
    if not parts:
        return "<root>"
    return ".".join(str(p) for p in parts)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Bring the database schema up to date on startup.

    Ordering is load-bearing (BE-001-fix1) and follows three steps:

    1. ``CREATE EXTENSION IF NOT EXISTS pgcrypto`` runs FIRST so
       ``gen_random_uuid()`` exists at the time the recipes
       migration's ``CREATE TABLE`` parses its ``DEFAULT`` expression.
    2. The recipes migration runs NEXT so the ``recipes`` table is
       created with all CHECK constraints (title/description length,
       prep/cook 0-1440, servings 1-1000), the FK to ``users(id) ON
       DELETE CASCADE``, and the compound ``ix_recipes_owner_created_id``
       index. The migration's ``CREATE TABLE IF NOT EXISTS`` means that
       if ``create_all`` had run first (which is what BE-001 originally
       did), this step would silently no-op and the spec's CHECK
       constraints would never land on the live table — that was the
       bug CodeReviewer caught.
    3. ``Base.metadata.create_all`` runs LAST. It is a no-op for the
       ``recipes`` table (the migration already created it) and for
       ``users`` (already created in step 2's prelude below), and it
       picks up any ORM-mapped tables added in later milestones
       without further changes to this lifespan.

    The recipes migration references ``users(id)``, so the users
    table must exist before step 2. In production an Alembic
    migration creates it before the app boots; for dev/test startup
    against a fresh database we create it here explicitly via a
    filtered ``create_all`` over the ``users`` table only — kept as a
    prelude rather than relying on the closing ``create_all``, which
    would run too late for the FK.

    Idempotent: a second startup re-executes each step against an
    already-populated database without erroring.

    Failure modes are NOT swallowed. If any step raises (DB
    unreachable, ``users`` FK target missing, extension can't be
    installed), the lifespan logs the error and ``sys.exit(1)``s so
    the container fails fast instead of booting and serving 5xx.

    History: a 2026-05-16 incident on crm_v1 M5 surfaced when a
    similar block previously swallowed ``create_all`` failures with a
    ``log.warning`` and a ``yield``. The container booted "healthy,"
    served 500s for 4 hours, and shipped to a user before anyone
    noticed. The cost of a silent boot exceeded the cost of a hard
    boot fail by orders of magnitude.

    Production: swap this to Alembic migrations applied before the
    app boots; remove this lifespan block entirely.
    """
    import logging
    import sys
    log = logging.getLogger(__name__)
    try:
        async with engine.begin() as conn:
            # Step 1: pgcrypto extension. Required by the recipes
            # migration's ``id uuid DEFAULT gen_random_uuid()`` — the
            # default expression is parsed at CREATE TABLE time and
            # fails if the function does not yet exist. Wrapped in
            # ``run_sync`` rather than calling ``conn.execute`` so the
            # whole lifespan goes through the same sync-bridge surface
            # — keeps the unit-test recording-connection happy.
            def _create_pgcrypto(sync_conn) -> None:
                sync_conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

            await conn.run_sync(_create_pgcrypto)

            # Step 2 prelude: ensure the ``users`` table exists so the
            # recipes migration's FK to ``users(id)`` can resolve.
            # Filtered ``create_all`` over just the User table — does
            # NOT create the recipes table here (that's the migration's
            # job; doing it now would make the migration's IF NOT
            # EXISTS a no-op and the CHECK constraints would never
            # land — the BE-001 bug this fix exists to correct).
            if "users" in Base.metadata.tables:
                users_table = Base.metadata.tables["users"]

                def _create_users_only(sync_conn) -> None:
                    Base.metadata.create_all(sync_conn, tables=[users_table])

                await conn.run_sync(_create_users_only)

            # Step 2: recipes migration. Creates the recipes table
            # WITH all CHECK constraints, FK ON DELETE CASCADE, and
            # the compound sort index. Idempotent — uses CREATE TABLE
            # IF NOT EXISTS and CREATE INDEX IF NOT EXISTS so re-runs
            # are no-ops.
            await conn.run_sync(recipes_migration.run_migration)

            # Step 3: full ``create_all`` for any other ORM-mapped
            # tables. No-op for users (created above) and for recipes
            # (created by the migration). This is the safety net that
            # picks up future ORM models without requiring edits to
            # this lifespan.
            await conn.run_sync(Base.metadata.create_all)
        log.info("Database tables ensured on startup")
    except Exception as e:
        log.error(
            f"Lifespan startup migration failed ({type(e).__name__}: {e}). "
            f"Refusing to boot — the container would otherwise serve "
            f"5xx on every DB-backed route. Check that the db service "
            f"is reachable, that the pgcrypto extension can be "
            f"installed, and that all model modules import cleanly "
            f"(see app/models/__init__.py)."
        )
        # Flush stderr/stdout so the log line lands in `docker logs`
        # before the process dies — without this the failure can be
        # invisible from outside.
        sys.stderr.flush()
        sys.stdout.flush()
        # Force-exit instead of raising: ``raise`` inside a lifespan
        # context manager just gets swallowed by FastAPI/Starlette
        # and the server still starts. ``sys.exit`` ensures uvicorn
        # actually terminates and docker marks the container failed.
        sys.exit(1)
    yield


# Explicit annotation so mypy doesn't infer this from the surrounding
# `app/` package (mypy would otherwise flag every `app.add_middleware`
# / `app.include_router` call as "Module has no attribute X").
app: FastAPI = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

_cors_origins: list[str] = []
if settings.environment == "development":
    _cors_origins = ["*"]
elif settings.environment in ("staging", "production"):
    _cors_origins = [settings.app_base_url]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _include_routers() -> None:
    """Auto-discover routers in app/api/routes/.

    Drop a new file at app/api/routes/<feature>.py that exports
    ``router: APIRouter`` (declare its own ``prefix="/<feature>"`` and
    ``tags=[...]``) and it will be mounted under
    ``settings.api_v1_prefix`` automatically — no edit to this file is
    required. This is the skeleton's only contract for adding new
    domain endpoints; see SKELETON.md.

    Warns loudly when a module is present but doesn't expose
    ``router`` — silent skip-on-mismatch was the route-mounting
    failure mode that hid bugs from the engineer.
    """
    import logging
    log = logging.getLogger(__name__)

    for mod_info in pkgutil.iter_modules(_routes_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{_routes_pkg.__name__}.{mod_info.name}")
        router_obj = getattr(module, "router", None)
        if isinstance(router_obj, APIRouter):
            app.include_router(router_obj, prefix=settings.api_v1_prefix)
        elif router_obj is None:
            log.warning(
                "[skeleton] %s.py has no `router` attribute — "
                "expected `router = APIRouter(prefix='/<feature>', tags=[...])`. "
                "See SKELETON.md.",
                mod_info.name,
            )
        else:
            log.warning(
                "[skeleton] %s.py exports `router` but it's not an APIRouter "
                "(got %s). See SKELETON.md.",
                mod_info.name,
                type(router_obj).__name__,
            )


_include_routers()


# ── Standard error-envelope handlers (BE-006-fix1) ──────────────────


@app.exception_handler(RequestValidationError)
async def _request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Remap Pydantic body validation 422 → 400 with the standard envelope.

    FastAPI's default surfaces ``RequestValidationError`` as ``422`` with
    ``{"detail": [{"loc": [...], "msg": ..., "type": ...}, ...]}``. The
    cross-cutting spec mandates ``400`` with
    ``{"error": "validation_failed", "message": ..., "field_errors":
    {<field>: <msg>, ...}}``. The path-UUID coercion in
    ``recipes._validate_recipe_id`` already raises 400 directly (an
    ``HTTPException`` that lands in the HTTPException handler below), so
    this handler only ever fires for body/query/header validation —
    primarily POST and PUT body failures.

    ``field_errors`` keys are flattened: nested fields like
    ``('body', 'ingredients', 2)`` collapse to ``'ingredients.2'`` so
    the SPA can highlight the offending input.
    """
    field_errors: dict[str, str] = {}
    for err in exc.errors():
        field = _field_name_from_loc(tuple(err.get("loc", ())))
        # If multiple errors target the same field, keep the first one.
        # Pydantic produces deterministic ordering so this matches the
        # field declaration order in the schema.
        field_errors.setdefault(field, err.get("msg", "invalid"))
    return JSONResponse(
        status_code=400,
        content={
            "error": "validation_failed",
            "message": "Request body failed validation.",
            "field_errors": field_errors,
        },
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Wrap HTTPException bodies in the standard envelope.

    Two cases for ``exc.detail``:

    * **str** (e.g. ``HTTPException(404, detail='recipe_not_found')``):
      treat the string as the machine code. Look up a human-readable
      message in ``_HTTP_ERROR_MESSAGE_MAP``; fall back to using the
      code itself as the message (so unknown codes still travel in the
      envelope without losing information).
    * **dict** (e.g. ``HTTPException(401, detail={'error':
      'unauthenticated'})`` — the shape ``app.core.auth`` already uses):
      preserve the existing ``error`` key and add a ``message`` if one
      isn't already supplied. Extra keys (``field_errors`` etc.) flow
      through verbatim.

    Anything else gets coerced to a string and travels as the
    ``message`` with ``error="error"``.
    """
    detail = exc.detail

    if isinstance(detail, dict):
        body: dict[str, Any] = {}
        error_code = detail.get("error") or "error"
        body["error"] = error_code
        body["message"] = detail.get("message") or _humanize_for_code(
            error_code
        )
        for k, v in detail.items():
            if k not in ("error", "message"):
                body[k] = v
    elif isinstance(detail, str):
        body = {
            "error": detail,
            "message": _humanize_for_code(detail),
        }
    else:
        body = {"error": "error", "message": str(detail)}

    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code, content=body, headers=headers
    )


@app.exception_handler(OperationalError)
async def _operational_error_handler(
    request: Request, exc: OperationalError
) -> JSONResponse:
    """Translate SQLAlchemy ``OperationalError`` to 503 ``db_unavailable``.

    ``OperationalError`` covers DB-readonly windows, connection drops,
    and "could not connect to server" — the cross-cutting spec lists
    these as the 503 surface for create/update/delete/list. Without
    this handler they bubble through the default 500 handler and the
    SPA can't tell "real bug" from "transient infra".

    The underlying exception is logged at ERROR level (with the SQL /
    driver message in the log) but is NEVER included in the response
    body — leaking driver-level details would be an information
    disclosure issue.
    """
    log = logging.getLogger(__name__)
    log.exception("db_unavailable", exc_info=exc)
    return JSONResponse(
        status_code=503,
        content={
            "error": "db_unavailable",
            "message": "Database is temporarily unavailable.",
        },
    )


@app.get("/health")
async def health():
    return {"status": "healthy"}
