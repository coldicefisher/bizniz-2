"""Unit tests for :func:`app.repositories.user_repository.get_user_by_id`.

The function is a thin SQLAlchemy 2.0 ``select`` wrapper, so the
behaviours worth verifying are:

* It returns the matching :class:`User` row when one exists.
* It returns ``None`` when no row matches (so callers can drive
  mirror auto-create on a JWT whose ``sub`` has no local row yet).
* It does NOT swallow DB exceptions — they must bubble so the
  route layer can translate to ``503``.

CITEXT (used by the production ``User.email`` column) has no
sqlite type compiler, so these tests build a minimal in-memory
SQLite copy of the table with plain ``String`` for ``email`` and
``UUID``-as-string for the PK. This keeps the test fast and free
of the docker postgres dependency while still exercising the real
ORM query path.
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.repositories.user_repository import get_user_by_id


class _TestBase(DeclarativeBase):
    """Isolated declarative base — does not touch the production
    metadata, so unrelated tables (e.g. the real ``users`` with
    CITEXT) never get created on the sqlite engine.
    """


class _SqliteUser(_TestBase):
    """sqlite-compatible stand-in for the production ``users`` table.

    Crucially, the table name is ``users`` and the column names match
    the production schema, so :func:`get_user_by_id` — which selects
    against ``app.models.user.User`` — issues the same SQL it would
    against Postgres. The ORM resolves the table by the model's
    ``__tablename__``, which both classes share.

    The id column uses :class:`PG_UUID(as_uuid=True)` (the same type
    as the production model) so SQLAlchemy's bind/result processors
    serialize UUID values identically on both sides. If we used a
    plain ``String(36)`` here, the production ``select(User).where(
    User.id == user_id)`` would bind ``user_id`` through PG_UUID's
    sqlite-fallback (32-char hex, no dashes) and miss our 36-char
    stored value.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True
    )
    email: Mapped[str] = mapped_column(sa.String(254), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="user")
    display_name: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    created_at: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    updated_at: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


@pytest.fixture
async def session() -> AsyncSession:
    """Fresh in-memory sqlite session with a sqlite-friendly ``users`` table.

    Built off ``_TestBase`` (NOT ``app.db.base.Base``) so the real
    ``User`` model's Postgres-only columns (CITEXT) never reach the
    create_all step.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_TestBase.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.unit
async def test_returns_user_when_id_matches(session: AsyncSession) -> None:
    """A row exists with the given id → the User instance is returned."""
    user_id = uuid.uuid4()
    session.add(
        _SqliteUser(
            id=user_id,
            email="alice@example.com",
            role="user",
            display_name="Alice",
        )
    )
    await session.commit()

    result = await get_user_by_id(session, user_id)

    assert result is not None
    assert result.id == user_id
    assert result.email == "alice@example.com"


@pytest.mark.unit
async def test_returns_none_when_id_missing(session: AsyncSession) -> None:
    """No row with that id → None (so callers can auto-create)."""
    # Seed an unrelated row to confirm the WHERE clause discriminates,
    # not just that the table is empty.
    session.add(
        _SqliteUser(
            id=uuid.uuid4(),
            email="someone@example.com",
            role="user",
        )
    )
    await session.commit()

    missing = uuid.uuid4()
    result = await get_user_by_id(session, missing)

    assert result is None


@pytest.mark.unit
async def test_returns_none_on_empty_table(session: AsyncSession) -> None:
    """Empty users table → None, not an exception."""
    result = await get_user_by_id(session, uuid.uuid4())
    assert result is None


@pytest.mark.unit
async def test_db_exception_bubbles_unwrapped(session: AsyncSession) -> None:
    """The function must not swallow SQLAlchemy errors.

    Drop the ``users`` table out from under the open session, then
    issue the lookup. SQLAlchemy will raise an OperationalError; the
    repository function MUST let it propagate (per the docstring's
    exception contract — the route layer translates this to 503).
    """
    await session.execute(sa.text("DROP TABLE users"))
    await session.commit()

    with pytest.raises(OperationalError):
        await get_user_by_id(session, uuid.uuid4())
