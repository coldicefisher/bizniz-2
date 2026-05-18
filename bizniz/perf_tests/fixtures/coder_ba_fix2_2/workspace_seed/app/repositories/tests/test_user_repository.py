"""Unit tests for :mod:`app.repositories.user_repository`.

The repository's surface is two functions:

* :func:`get_user_by_id` — async, returns the local users row matching
  the supplied FusionAuth ``sub``, or ``None``.
* :func:`upsert_user_mirror` — synchronous, idempotent ``INSERT ... ON
  CONFLICT (id) DO NOTHING ... RETURNING`` against the local mirror,
  with a typed exception when the email-unique constraint fires for a
  different PK.

These tests exercise both functions against the live Postgres test
container because the production model uses CITEXT and the upsert relies
on Postgres-only ``ON CONFLICT`` semantics — neither has a sqlite
equivalent. The fixture creates the ``citext`` extension and the
``users`` table once per test session and wraps each test in a
BEGIN/ROLLBACK envelope so writes auto-clean without touching the
schema.

If ``DATABASE_URL`` is unset (e.g. running unit-only outside the
compose stack), the file is skipped wholesale: the contract under test
is the Postgres ``ON CONFLICT`` shape, and asserting it against sqlite
would be a lie.

Test #5 (``test_upsert_idempotent_on_id_conflict``) is the load-bearing
guard against future "helpful" refactors that turn the insert-only
mirror into a real UPSERT — that would break the
"role from JWT, not from column" contract.

Test #9 (``test_upsert_db_exceptions_bubble``) is mock-driven (the
function must not swallow broad exceptions) and runs even when
Postgres is unavailable.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)

from app.db.base import Base
from app.models.user import User
from app.repositories.user_repository import (
    DuplicateEmailInMirror,
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

    Spins a fresh async engine per-test (asyncpg connection pools are
    bound to the event loop that created them, so reusing across
    pytest-asyncio tests trips ``got Future attached to a different
    loop``). Ensures the ``citext`` extension exists and the ``users``
    table is created — both idempotent, so concurrent test runs and
    fresh-stack boots converge on the same schema.

    Per-test isolation: opens an outer transaction, binds an
    AsyncSession with ``join_transaction_mode='create_savepoint'`` so
    internal flushes/commits from the function-under-test stay within
    the outer transaction. The outer transaction is rolled back at
    fixture teardown, so writes disappear without dropping the schema.
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


# --- get_user_by_id ---------------------------------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_get_user_by_id_returns_none_when_absent(
    session: AsyncSession,
) -> None:
    """Fresh UUID with no matching row → ``None`` (not an exception)."""
    fresh = uuid.uuid4()
    result = await get_user_by_id(session, fresh)
    assert result is None


@pytestmark_pg_required
@pytest.mark.unit
async def test_get_user_by_id_returns_user_when_present(
    session: AsyncSession,
) -> None:
    """A row exists with the given id → the User instance is returned."""
    user_id = uuid.uuid4()
    session.add(
        User(
            id=user_id,
            email=f"present-{user_id}@example.com",
            role="user",
            display_name="Present",
        )
    )
    await session.flush()

    result = await get_user_by_id(session, user_id)

    assert result is not None
    assert result.id == user_id
    assert result.email == f"present-{user_id}@example.com"


# --- upsert_user_mirror -----------------------------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_upsert_inserts_new_user(session: AsyncSession) -> None:
    """Fresh id+email → returned User has id=fa_user_id, lowercased email,
    role='user', display_name=None. Follow-up get_user_by_id finds it.
    """
    fa_id = uuid.uuid4()
    email = f"new-{fa_id}@example.com"

    inserted = await session.run_sync(
        lambda s: upsert_user_mirror(s, fa_id, email)
    )

    assert inserted.id == fa_id
    assert inserted.email == email  # already lower-case
    assert inserted.role == "user"
    assert inserted.display_name is None

    fetched = await get_user_by_id(session, fa_id)
    assert fetched is not None
    assert fetched.id == fa_id


@pytestmark_pg_required
@pytest.mark.unit
async def test_upsert_lowercases_email(session: AsyncSession) -> None:
    """Mixed-case email is normalized to lowercase before persistence."""
    fa_id = uuid.uuid4()
    raw = f"Foo-{fa_id}@BAR.com"
    expected = raw.lower()

    inserted = await session.run_sync(
        lambda s: upsert_user_mirror(s, fa_id, raw)
    )

    assert inserted.email == expected


@pytestmark_pg_required
@pytest.mark.unit
async def test_upsert_idempotent_on_id_conflict(
    session: AsyncSession,
) -> None:
    """Second call with the same id returns the ORIGINAL row.

    Locks in the contract that the mirror is INSERT-ONLY on conflict —
    a future refactor that turns this into a real UPSERT would change
    display_name on the second call and break this assertion. The point
    is to defend against that drift.
    """
    fa_id = uuid.uuid4()
    email = f"idem-{fa_id}@example.com"

    first = await session.run_sync(
        lambda s: upsert_user_mirror(s, fa_id, email, display_name="First")
    )
    assert first.display_name == "First"

    second = await session.run_sync(
        lambda s: upsert_user_mirror(s, fa_id, email, display_name="Second")
    )

    # Insert-only: the second call must return the ORIGINAL row, with
    # display_name still 'First', NOT 'Second'.
    assert second.id == fa_id
    assert second.display_name == "First"


@pytestmark_pg_required
@pytest.mark.unit
async def test_upsert_with_display_name(session: AsyncSession) -> None:
    """Passing display_name binds it on insert."""
    fa_id = uuid.uuid4()
    email = f"alice-{fa_id}@example.com"

    inserted = await session.run_sync(
        lambda s: upsert_user_mirror(s, fa_id, email, display_name="Alice")
    )

    assert inserted.display_name == "Alice"


@pytestmark_pg_required
@pytest.mark.unit
async def test_upsert_with_role(session: AsyncSession) -> None:
    """Passing role binds it on insert (informational mirror)."""
    fa_id = uuid.uuid4()
    email = f"admin-{fa_id}@example.com"

    inserted = await session.run_sync(
        lambda s: upsert_user_mirror(s, fa_id, email, role="admin")
    )

    assert inserted.role == "admin"


@pytestmark_pg_required
@pytest.mark.unit
async def test_upsert_duplicate_email_different_id_raises(
    session: AsyncSession,
) -> None:
    """Same email + different id → :class:`DuplicateEmailInMirror`.

    Insert user A with email 'a@b.c'. Attempt to insert user B with the
    same email but a different id. The email-unique constraint fires
    (id ON CONFLICT does NOT, since the PKs differ); the function
    translates this to the typed exception so the auth router can
    surface 500 ``duplicate_email_in_mirror``.
    """
    id_a = uuid.uuid4()
    id_b = uuid.uuid4()
    shared_email = f"dup-{id_a}@example.com"

    await session.run_sync(
        lambda s: upsert_user_mirror(s, id_a, shared_email)
    )

    with pytest.raises(DuplicateEmailInMirror) as exc_info:
        await session.run_sync(
            lambda s: upsert_user_mirror(s, id_b, shared_email)
        )

    assert exc_info.value.email == shared_email
    assert exc_info.value.attempted_id == id_b


# --- exception bubbling (mock-based, runs without Postgres) -----------


@pytest.mark.unit
def test_upsert_db_exceptions_bubble() -> None:
    """A non-IntegrityError DB failure (OperationalError) propagates unwrapped.

    The repository contract is that connection drops, statement
    timeouts, etc. bubble untouched so the route layer can translate
    them to 503. The function must never swallow these to a typed
    exception or — worse — to None.
    """
    fa_id = uuid.uuid4()
    boom = OperationalError("stmt", {}, Exception("connection lost"))

    session = MagicMock()
    session.execute.side_effect = boom

    with pytest.raises(OperationalError) as exc_info:
        upsert_user_mirror(session, fa_id, "alice@example.com")

    assert exc_info.value is boom
    # No rollback on the unhandled path — transaction state belongs to
    # the caller (the route layer), not to the repository.
    session.rollback.assert_not_called()
