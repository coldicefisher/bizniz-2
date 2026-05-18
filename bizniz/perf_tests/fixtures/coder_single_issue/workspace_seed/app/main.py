import importlib
import pkgutil
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import engine
import app.api.routes as _routes_pkg
import app.models  # noqa: F401 — triggers auto-import of every model module

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create database tables on startup. Fail-fast if the DB is
    unreachable or ``create_all`` raises — better to crash the
    container at boot than to serve 500s for hours.

    Idempotent — ``create_all`` skips tables that already exist.

    Tests that don't want a real database should mock
    ``engine.begin`` or run with a sqlite-memory engine; the
    skeleton's test fixtures already do this.

    History: a 2026-05-16 incident on crm_v1 M5 surfaced when this
    block previously swallowed ``create_all`` failures with a
    ``log.warning`` and a ``yield``. The container booted "healthy,"
    served 500s for 4 hours, and shipped to a user before anyone
    noticed. The cost of a silent boot exceeded the cost of a hard
    boot fail by orders of magnitude.

    Production: swap this to Alembic migrations applied before
    the app boots; remove this lifespan block entirely.
    """
    import logging
    import sys
    log = logging.getLogger(__name__)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("Database tables ensured on startup")
    except Exception as e:
        log.error(
            f"Lifespan create_all failed ({type(e).__name__}: {e}). "
            f"Refusing to boot — the container would otherwise serve "
            f"5xx on every DB-backed route. Check that the db service "
            f"is reachable and that all model modules import cleanly "
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


@app.get("/health")
async def health():
    return {"status": "healthy"}
