"""Behavioural unit tests for the ``app.models.user.User`` model.

These tests exercise the User row's runtime behaviour at the DB
level — server-managed defaults on flush, CITEXT case-insensitive
unique constraint, and the role CheckConstraint. Schema-level
metadata assertions live in ``tests/unit/test_user_model.py``;
this file complements that by actually round-tripping rows
through a session.

CITEXT and the ``role IN (...)`` CheckConstraint are Postgres-only
artefacts, so all three tests require the project's docker postgres
sidecar (``DATABASE_URL`` set to an asyncpg URL). Without it, every
test in this module skips — the User model literally cannot be
materialised on sqlite (CITEXT has no sqlite type compiler).

The ``pg_session`` fixture creates ``citext`` and the ``users``
table inside its own outer transaction and rolls back at teardown.
That keeps the live Postgres schema untouched (consistent with the
top-level conftest contract: never drop tables on the live DB).
"""
from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.user import User


POSTGRES_ONLY = (
    "requires Postgres (CITEXT + role CHECK constraint are "
    "Postgres-only); set DATABASE_URL to an asyncpg URL to enable"
)


@pytest.fixture
async def pg_session():
    """Per-test transactional session against the live Postgres DB.

    Wraps each test in BEGIN/ROLLBACK and provisions the ``citext``
    extension + ``users`` table inside that transaction so the
    teardown ROLLBACK cleans them up without mutating the persistent
    schema. Skips if DATABASE_URL is unset or non-Postgres.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url or not db_url.startswith("postgresql"):
        pytest.skip(POSTGRES_ONLY)
    engine = create_async_engine(db_url, echo=False)
    try:
        async with engine.connect() as conn:
            outer_trans = await conn.begin()
            try:
                # Postgres DDL is transactional — both statements roll
                # back cleanly when the outer transaction does.
                await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
                await conn.run_sync(
                    lambda sync_conn: User.__table__.create(sync_conn, checkfirst=True)
                )
                session = AsyncSession(
                    bind=conn,
                    expire_on_commit=False,
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


@pytest.mark.unit
async def test_user_instantiation_defaults(pg_session: AsyncSession) -> None:
    """Server-managed defaults populate on flush.

    Inserting a User with only ``id`` and ``email`` set must leave
    ``role='user'`` (server_default), ``display_name=None``, and
    both timestamps populated by ``now()``.
    """
    user = User(id=uuid.uuid4(), email="Foo@Example.com")
    pg_session.add(user)
    await pg_session.flush()
    await pg_session.refresh(user)

    assert user.role == "user"
    assert user.display_name is None
    assert user.created_at is not None
    assert user.updated_at is not None


@pytest.mark.unit
async def test_email_case_insensitive_unique(pg_session: AsyncSession) -> None:
    """CITEXT makes the email unique constraint case-insensitive.

    Inserting ``alice@example.com`` then ``ALICE@example.com`` must
    raise IntegrityError on the second flush — CITEXT compares
    case-insensitively, so the unique index sees a collision.
    """
    first = User(id=uuid.uuid4(), email="alice@example.com")
    pg_session.add(first)
    await pg_session.flush()

    second = User(id=uuid.uuid4(), email="ALICE@example.com")
    pg_session.add(second)
    with pytest.raises(IntegrityError):
        await pg_session.flush()


@pytest.mark.unit
async def test_role_check_constraint(pg_session: AsyncSession) -> None:
    """CheckConstraint rejects any role outside the allowed set.

    Inserting a User with ``role='invalid_role'`` must raise
    IntegrityError on flush — the table's CHECK clause limits
    role to {'user', 'admin', 'super_admin'}.
    """
    user = User(
        id=uuid.uuid4(),
        email="bob@example.com",
        role="invalid_role",
    )
    pg_session.add(user)
    with pytest.raises(IntegrityError):
        await pg_session.flush()
