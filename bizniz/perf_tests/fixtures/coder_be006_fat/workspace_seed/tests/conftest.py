"""Test fixtures for the FastAPI skeleton.

Auth is delegated to FusionAuth in production. For unit tests, we
mock the JWT validation so tests don't need a running FusionAuth
instance. The ``auth_headers`` and ``admin_headers`` fixtures
produce Bearer tokens that the mocked ``get_current_user`` accepts.

Roles come from the JWT, not from a local table — so the role
fixtures here just record which roles to put in the mock JWT
claims, not into a local Role/UserRole table that no longer exists.

**Critical contract (do not violate):**

- ``db_session`` is for **pure-Python unit tests only** — uses
  sqlite-memory which dies with the engine.
- For tests that exercise the **live FastAPI app against real
  Postgres**, use ``live_postgres_session`` below. It uses
  BEGIN/ROLLBACK so writes auto-clean per-test without touching
  the schema.
- **NEVER call ``Base.metadata.drop_all()`` on a live database.**
  The pre-2026-05-16 pattern of per-test create_all + drop_all
  against the production Postgres left the DB tables-less after
  the suite finished, and the next process boot's lifespan
  silently failed to recreate them, shipping a broken app. This
  is a hard contract — violations should be caught in code review.
"""
import os
import pytest
import uuid
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.user import User

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_session():
    """Pure-Python unit-test session — sqlite in-memory.

    Use for tests that exercise a single class or pure function
    against the ORM without needing the FastAPI app or real
    Postgres. ``Base.metadata.drop_all`` here is safe because the
    engine is in-memory and dies with the fixture.
    """
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def live_postgres_session():
    """Per-test transactional session against the live Postgres DB.

    Uses BEGIN/ROLLBACK so each test's writes are isolated and
    auto-cleaned. **Never drops tables** — the live DB schema must
    persist across tests AND into post-test app usage (the user
    will run the app against this DB after the suite finishes).

    Schema is assumed to already exist (created by the backend's
    lifespan ``create_all`` at boot, or by an earlier test that
    used the engine). If ``DATABASE_URL`` is unset, the test is
    skipped — this fixture exists for tests that opt into the
    live-stack contract.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; skipping live-stack test")
    engine = create_async_engine(db_url, echo=False)
    try:
        async with engine.connect() as conn:
            outer_trans = await conn.begin()
            try:
                session = AsyncSession(
                    bind=conn, expire_on_commit=False,
                    # join_transaction_mode keeps the session writes
                    # within the outer transaction we just opened, so
                    # rollback below unwinds everything the test did.
                    join_transaction_mode="create_savepoint",
                )
                try:
                    yield session
                finally:
                    await session.close()
            finally:
                await outer_trans.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def client(db_session):
    """ASGI in-process client backed by sqlite-memory.

    Fast — no docker stack required. Use for tests of pure-Python
    code paths (helpers, schema validation, ORM serialization) that
    happen to need an app context. **Do NOT use for tests that
    assert real route-handler behavior end-to-end**; sqlite-memory
    hides Postgres-specific failures (missing tables, dialect quirks,
    extension absence). Use ``live_client`` for that.
    """
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def live_client(live_postgres_session):
    """ASGI in-process client backed by the live Postgres database.

    Combines the speed of ASGI transport (no real HTTP, no docker
    network) with the correctness of real Postgres (real types, real
    extensions, real lifespan startup behavior). Each test runs
    inside a BEGIN/ROLLBACK envelope via ``live_postgres_session``,
    so writes auto-clean without dropping the schema.

    Use for **any route test that touches the DB.** This catches the
    class of bug the 2026-05-16 crm_v1 incident exposed: route tests
    against sqlite-memory passed while the production Postgres
    schema was empty — sqlite doesn't catch missing-table failures
    on the real DB.

    Skips if ``DATABASE_URL`` is unset, so unit-only CI environments
    aren't forced to provision Postgres.
    """
    async def override_get_db():
        yield live_postgres_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def test_user(db_session):
    """Create a test user in the local DB (simulating FusionAuth sync).

    Roles come from the JWT in production. For unit tests, the
    ``auth_headers`` fixture pairs this user with a mock token whose
    claims include ``roles=["user"]``.
    """
    user = User(
        user_id=uuid.uuid4(),
        email="test@example.com",
        first_name="Test",
        last_name="User",
        email_verified=False,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def admin_user(db_session):
    """Create an admin user in the local DB (simulating FusionAuth sync).

    The mocked JWT will include ``roles=["user", "admin"]``.
    """
    user = User(
        user_id=uuid.uuid4(),
        email="admin@example.com",
        first_name="Admin",
        last_name="User",
        email_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


def _mock_jwt_token(user_id: uuid.UUID) -> str:
    """Create a fake token string for testing.

    In production, FusionAuth issues RS256 JWTs. In tests, we mock
    the validation layer so the token content doesn't matter — only
    the user_id mapping matters.
    """
    return f"test-token-{user_id}"


@pytest.fixture
def auth_headers(test_user):
    """Bearer headers for a regular user — use with mocked auth."""
    return {"Authorization": f"Bearer {_mock_jwt_token(test_user.user_id)}"}


@pytest.fixture
def admin_headers(admin_user):
    """Bearer headers for an admin user — use with mocked auth."""
    return {"Authorization": f"Bearer {_mock_jwt_token(admin_user.user_id)}"}
