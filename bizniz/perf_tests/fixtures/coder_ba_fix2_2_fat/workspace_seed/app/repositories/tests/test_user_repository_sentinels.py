"""Sentinel tests for :mod:`app.repositories.user_repository`.

This file is intentionally separate from ``test_user_repository.py`` so the
security-critical contracts on the local users mirror are pinned in one
auditable place. Each test is a tripwire against a specific class of
regression flagged by QualityEngineer:

* :func:`test_insert_only_on_conflict_returns_original_row` — locks the
  ``ON CONFLICT (id) DO NOTHING`` contract. A future refactor that
  switches to ``DO UPDATE`` would let stale JWT-derived columns
  overwrite the persisted row and break the 'role from JWT, not from
  column' invariant.
* :func:`test_email_lowercased_on_insert_and_persisted` — pins the
  email-normalization layer at insert time so the case-insensitive
  unique constraint can't be bypassed by mixed-case input.
* :func:`test_db_exceptions_bubble_unwrapped` — ensures non-IntegrityError
  DB failures (OperationalError etc.) propagate unwrapped per the
  no-swallow rule; route layer needs the raw error to translate to 503.
* :func:`test_role_check_constraint_rejects_invalid` — exercises the
  Postgres CHECK constraint on ``role`` so a typo'd 'hacker' role can
  never persist.
* :func:`test_default_role_user_on_insert` — confirms the server-side
  default applies when callers omit ``role`` (Core insert path).
* :func:`test_case_insensitive_email_lookup` — proves CITEXT semantics
  on the live Postgres backend; an SQLite-only test would lie about
  this.

These tests share the live-Postgres pattern from
``test_user_repository.py`` (per-test BEGIN/ROLLBACK envelope) so writes
auto-clean and the schema isn't touched. If ``DATABASE_URL`` is unset
the postgres-dependent tests skip with a clear reason; the mock-based
:func:`test_db_exceptions_bubble_unwrapped` runs unconditionally.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)

from app.db.base import Base
from app.models.user import User
from app.repositories.user_repository import (
    get_user_by_id,
    upsert_user_mirror,
)

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark_pg_required = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set; Postgres-only test (CITEXT + ON CONFLICT)",
)


@pytest.fixture
async def session() -> AsyncSession:
    """Per-test transactional AsyncSession against the live Postgres DB.

    Mirrors the fixture in ``test_user_repository.py``: spins a fresh
    async engine per-test (asyncpg pools bind to the creating event
    loop), ensures CITEXT + the ``users`` table exist, then wraps the
    test body in BEGIN/ROLLBACK so writes auto-clean without dropping
    the schema.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set; Postgres-only test")
    engine = create_async_engine(DATABASE_URL, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("CREATE EXTENSION IF NOT EXISTS citext")
            )
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                s = AsyncSession(
                    bind=conn,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                try:
                    yield s
                finally:
                    await s.close()
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


# --- 1. insert_only_on_conflict ---------------------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_insert_only_on_conflict_returns_original_row(
    session: AsyncSession,
) -> None:
    """Second upsert with same id but different display_name returns ORIGINAL.

    This is the load-bearing sentinel against a future refactor that
    'helpfully' turns ``ON CONFLICT (id) DO NOTHING`` into
    ``ON CONFLICT (id) DO UPDATE``. Such a refactor would let
    JWT-derived columns silently overwrite the persisted row, breaking
    the 'role from JWT, not from column' contract.

    Assertions:
      * The returned row from the second call has display_name='Alice'
        (the ORIGINAL value), NOT 'RENAMED'.
      * The persisted row, fetched by id afterwards, also still has
        display_name='Alice' — proving the second upsert never wrote.
    """
    fa_id = uuid.uuid4()
    email = f"alice-{fa_id}@example.com"

    first = await session.run_sync(
        lambda s: upsert_user_mirror(
            s, fa_id, email, display_name="Alice"
        )
    )
    assert first.display_name == "Alice"

    second = await session.run_sync(
        lambda s: upsert_user_mirror(
            s, fa_id, email, display_name="RENAMED"
        )
    )

    assert second.id == fa_id
    assert second.display_name == "Alice", (
        "ON CONFLICT (id) DO NOTHING contract broken: second upsert "
        "with a different display_name returned the new value, which "
        "means the mirror became a real UPSERT."
    )

    fetched = await get_user_by_id(session, fa_id)
    assert fetched is not None
    assert fetched.display_name == "Alice", (
        "Persisted row was mutated by the second upsert — the "
        "mirror is supposed to be insert-only."
    )


# --- 2. email_lowercased_on_insert ------------------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_email_lowercased_on_insert_and_persisted(
    session: AsyncSession,
) -> None:
    """Mixed-case email is normalized at insert AND the persisted row carries it.

    The upsert helper lowercases the email before issuing the INSERT
    (the case-insensitive unique constraint must not be bypassable
    via mixed case). This test asserts both:
      * The returned row's email is lowercase.
      * The row read back from the DB by id carries the lowercase value.
    """
    fa_id = uuid.uuid4()
    raw = f"MixedCase-{fa_id}@X.COM"
    expected = raw.lower()

    inserted = await session.run_sync(
        lambda s: upsert_user_mirror(
            s, fa_id, raw, display_name="X"
        )
    )

    assert inserted.email == expected, (
        f"upsert_user_mirror returned non-lowercased email "
        f"{inserted.email!r}; expected {expected!r}"
    )

    fetched = await get_user_by_id(session, fa_id)
    assert fetched is not None
    assert fetched.email == expected, (
        f"Persisted email {fetched.email!r} is not the lowercased "
        f"form {expected!r} — normalization layer regressed."
    )


# --- 3. db_exceptions_bubble (mock-based, runs without Postgres) ------


@pytest.mark.unit
def test_db_exceptions_bubble_unwrapped() -> None:
    """Non-IntegrityError DB failures propagate unwrapped.

    The repository contract is that connection drops, read-only mode,
    statement timeouts, etc. bubble as raw SQLAlchemy errors so the
    route layer can translate them to 503. The function must NEVER
    swallow these to a typed exception (DuplicateEmailInMirror,
    RuntimeError, generic HTTPException, etc.) — losing the original
    traceback would erase the only diagnostic the route logger has.

    Implementation note: this test is mock-based and runs even when
    Postgres is unavailable; the no-swallow contract is independent
    of the DB backend.
    """
    fa_id = uuid.uuid4()
    boom = OperationalError("SELECT 1", {}, Exception("readonly"))

    session = MagicMock()
    session.execute.side_effect = boom

    with pytest.raises(OperationalError) as exc_info:
        upsert_user_mirror(session, fa_id, "alice@example.com")

    assert exc_info.value is boom, (
        "OperationalError was re-wrapped; the original exception "
        "instance must propagate unchanged."
    )
    # The repository must not rollback on the unhandled path — that
    # transaction belongs to the caller (the route layer).
    session.rollback.assert_not_called()


# --- 4. role_check_constraint_rejects_invalid -------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_role_check_constraint_rejects_invalid(
    session: AsyncSession,
) -> None:
    """Inserting role='hacker' trips the CHECK constraint.

    The migration declares ``CHECK (role IN ('user', 'admin',
    'super_admin'))``; a row with role='hacker' must fail at the
    DB layer regardless of any app-level validation, so the constraint
    is a defense-in-depth tripwire against a future code path that
    skips Pydantic validation.
    """
    fa_id = uuid.uuid4()

    def _try_insert(s: sa.orm.Session) -> None:
        s.add(
            User(
                id=fa_id,
                email=f"hacker-{fa_id}@example.com",
                role="hacker",
                display_name="Hacker",
            )
        )
        s.flush()

    with pytest.raises(IntegrityError):
        await session.run_sync(_try_insert)


# --- 5. default_role_user_on_insert -----------------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_default_role_user_on_insert(
    session: AsyncSession,
) -> None:
    """A row INSERTed without specifying role gets the DB default 'user'.

    Uses a Core insert (not the ORM, which would bind ``role=None``
    explicitly) so the server_default fires. This proves the
    'default role=user' invariant is enforced at the DB layer — a
    future caller that forgets to set role can never accidentally
    create a no-role row that the JWT validator then has to backfill.
    """
    fa_id = uuid.uuid4()
    email = f"defaultrole-{fa_id}@example.com"

    def _insert(s: sa.orm.Session) -> None:
        s.execute(
            sa.insert(User.__table__).values(
                id=fa_id,
                email=email,
            )
        )
        s.flush()

    await session.run_sync(_insert)

    fetched = await get_user_by_id(session, fa_id)
    assert fetched is not None
    assert fetched.role == "user", (
        f"Expected DB-default role='user' on insert without role, "
        f"got {fetched.role!r}"
    )


# --- 6. case_insensitive_email_lookup ---------------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_case_insensitive_email_lookup(
    session: AsyncSession,
) -> None:
    """A row inserted with lowercase email is findable by uppercase query.

    The model declares ``email`` as CITEXT(254), so equality is
    case-insensitive at the type level — no per-call ``lower()``
    plumbing required. This test exercises that semantic against the
    live Postgres backend (the only one that supports CITEXT) by
    inserting 'alice@x.com' and querying with 'ALICE@X.COM'; exactly
    one row must come back.
    """
    fa_id = uuid.uuid4()
    stored_email = f"alice-{fa_id}@x.com"
    upper_email = stored_email.upper()

    session.add(
        User(
            id=fa_id,
            email=stored_email,
            role="user",
            display_name="Alice",
        )
    )
    await session.flush()

    result = await session.execute(
        sa.select(User).where(User.email == upper_email)
    )
    rows = result.scalars().all()

    assert len(rows) == 1, (
        f"CITEXT case-insensitive lookup returned {len(rows)} rows "
        f"for {upper_email!r}; expected 1"
    )
    assert rows[0].id == fa_id
