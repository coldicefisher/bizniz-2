"""Unit tests for :func:`app.repositories.recipes.create_recipe`.

The repository contract under test:

* ``create_recipe`` is synchronous and takes a ``Session``.
* It constructs a :class:`Recipe` from the validated ``RecipeCreate``
  data plus the server-derived ``owner_id``.
* It flushes (not commits) so the server defaults
  (``id``, ``created_at``, ``updated_at``) populate, and refreshes the
  instance.
* The route layer owns the transaction boundary — same convention as
  :mod:`user_repository`.

Live-Postgres path (matches ``test_user_repository.py``): the schema
relies on ``gen_random_uuid()`` + ``now()`` defaults that sqlite can't
fake, and the FK to ``users(id)`` must be honored. We spin a fresh
async engine per-test, create the schema and the ``citext`` extension
(needed for the users mirror), wrap each test in BEGIN/ROLLBACK, and
exercise the function through ``session.run_sync`` because the
repository is synchronous.

If ``DATABASE_URL`` is unset (unit-only CI), the file is skipped — the
contract under test relies on Postgres-only DDL.
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
from app.models.recipe import Recipe
from app.models.user import User
from app.repositories.recipes import (
    create_recipe,
    delete_recipe_for_owner,
    get_recipe_for_owner,
    list_recipes_for_owner,
    update_recipe_for_owner,
)
from app.schemas.recipe import RecipeCreate

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark_pg_required = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set; Postgres-only test (gen_random_uuid + FK)",
)


def _valid_payload(**overrides) -> RecipeCreate:
    """Build a RecipeCreate with sensible defaults; override per-test."""
    payload = {
        "title": "Test Recipe",
        "description": "A description for testing the create_recipe repo.",
        "ingredients": ["flour", "water", "salt"],
        "instructions": ["mix", "bake"],
        "prep_time": 10,
        "cook_time": 20,
        "servings": 4,
    }
    payload.update(overrides)
    return RecipeCreate(**payload)


@pytest.fixture
async def session() -> AsyncSession:
    """Per-test transactional AsyncSession against the live Postgres DB.

    Spins a fresh async engine per-test (asyncpg connection pools are
    bound to the event loop that created them). Ensures the
    ``pgcrypto`` and ``citext`` extensions exist (the model layer for
    recipes needs ``gen_random_uuid()`` from pgcrypto; the users
    mirror needs ``citext``) and that all tables defined on
    ``Base.metadata`` exist.

    BEGIN/ROLLBACK envelope per the conftest contract: do NOT drop
    schema, just roll back the outer transaction so writes disappear.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set; Postgres-only test")
    engine = create_async_engine(DATABASE_URL, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("CREATE EXTENSION IF NOT EXISTS citext")
            )
            await conn.execute(
                sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto")
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


async def _insert_owner(session: AsyncSession) -> uuid.UUID:
    """Insert a users mirror row and return its id (FK target for recipes)."""
    owner_id = uuid.uuid4()
    session.add(
        User(
            id=owner_id,
            email=f"owner-{owner_id}@example.com",
            role="user",
            display_name="Owner",
        )
    )
    await session.flush()
    return owner_id


# --- create_recipe ----------------------------------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_create_recipe_inserts_row_and_populates_server_defaults(
    session: AsyncSession,
) -> None:
    """Happy path: returned Recipe has id / created_at / updated_at populated.

    The owner_id parameter is reflected; the data fields are persisted
    verbatim. ``id``, ``created_at``, ``updated_at`` come from server
    defaults (``gen_random_uuid()`` / ``now()``) — they must be
    populated on the returned instance after flush+refresh, not None.
    """
    owner_id = await _insert_owner(session)
    data = _valid_payload(title="Lemon Tart")

    created = await session.run_sync(
        lambda s: create_recipe(s, owner_id=owner_id, data=data)
    )

    assert created.id is not None
    assert isinstance(created.id, uuid.UUID)
    assert created.owner_id == owner_id
    assert created.title == "Lemon Tart"
    assert created.description == data.description
    assert created.ingredients == data.ingredients
    assert created.instructions == data.instructions
    assert created.prep_time == data.prep_time
    assert created.cook_time == data.cook_time
    assert created.servings == data.servings
    assert created.created_at is not None
    assert created.updated_at is not None


@pytestmark_pg_required
@pytest.mark.unit
async def test_create_recipe_owner_id_from_parameter_not_data(
    session: AsyncSession,
) -> None:
    """The owner_id stored on the row comes from the function arg.

    The :class:`RecipeCreate` schema rejects unknown fields via
    ``extra='forbid'``, but the repository contract also enforces it
    by sourcing owner_id only from the keyword argument — there's no
    code path that reads ``owner_id`` from ``data``. This test pins
    the contract: the owner_id on the persisted row equals the arg.
    """
    owner_id = await _insert_owner(session)
    data = _valid_payload()

    created = await session.run_sync(
        lambda s: create_recipe(s, owner_id=owner_id, data=data)
    )

    assert created.owner_id == owner_id


@pytestmark_pg_required
@pytest.mark.unit
async def test_create_recipe_row_persists_in_db(
    session: AsyncSession,
) -> None:
    """A subsequent SELECT in the same transaction finds the new row.

    Verifies the function actually writes to the session (not just
    constructs a transient ORM instance). The follow-up SELECT runs
    against the same BEGIN/ROLLBACK envelope so the row is visible
    here even though the function only flushed (did not commit).
    """
    owner_id = await _insert_owner(session)
    data = _valid_payload(title="Pumpkin Soup")

    created = await session.run_sync(
        lambda s: create_recipe(s, owner_id=owner_id, data=data)
    )

    fetched = await session.execute(
        sa.select(Recipe).where(Recipe.id == created.id)
    )
    row = fetched.scalar_one()
    assert row.id == created.id
    assert row.title == "Pumpkin Soup"
    assert row.owner_id == owner_id


@pytestmark_pg_required
@pytest.mark.unit
async def test_create_recipe_two_calls_distinct_ids(
    session: AsyncSession,
) -> None:
    """Rapid double-create from same owner → two rows with distinct ids.

    Mirrors the contract's ``double_submit_creates_two`` scenario:
    the server does not deduplicate; identical payloads produce
    distinct rows.
    """
    owner_id = await _insert_owner(session)
    data = _valid_payload(title="Same")

    first = await session.run_sync(
        lambda s: create_recipe(s, owner_id=owner_id, data=data)
    )
    second = await session.run_sync(
        lambda s: create_recipe(s, owner_id=owner_id, data=data)
    )

    assert first.id != second.id
    assert first.owner_id == second.owner_id == owner_id


@pytestmark_pg_required
@pytest.mark.unit
async def test_create_recipe_preserves_unicode(
    session: AsyncSession,
) -> None:
    """Unicode in title / ingredients / instructions round-trips byte-identical.

    Matches the contract's ``unicode_preserved`` scenario.
    """
    owner_id = await _insert_owner(session)
    data = _valid_payload(
        title="Soupe à l'oignon 🧅",
        description="French onion soup — 美味しい",
        ingredients=["oignons 🧅", "fromage 🧀"],
        instructions=["étape 1", "étape 2"],
    )

    created = await session.run_sync(
        lambda s: create_recipe(s, owner_id=owner_id, data=data)
    )

    assert created.title == "Soupe à l'oignon 🧅"
    assert created.description == "French onion soup — 美味しい"
    assert created.ingredients == ["oignons 🧅", "fromage 🧀"]
    assert created.instructions == ["étape 1", "étape 2"]


# --- exception bubbling (mock-based, runs without Postgres) -----------


@pytest.mark.unit
def test_create_recipe_db_exceptions_bubble() -> None:
    """A DB failure (OperationalError) on flush propagates unwrapped.

    The repository contract is that connection drops / statement
    timeouts bubble untouched so the route layer can translate them
    to 503. The function must never swallow these to None or to a
    typed exception.
    """
    owner_id = uuid.uuid4()
    data = _valid_payload()
    boom = OperationalError("stmt", {}, Exception("connection lost"))

    session = MagicMock()
    session.flush.side_effect = boom

    with pytest.raises(OperationalError) as exc_info:
        create_recipe(session, owner_id=owner_id, data=data)

    assert exc_info.value is boom
    # No rollback on the unhandled path — transaction state belongs to
    # the caller (the route layer), not to the repository.
    session.rollback.assert_not_called()


# --- list_recipes_for_owner -------------------------------------------


async def _create_recipe_async(
    session: AsyncSession, *, owner_id: uuid.UUID, **overrides
) -> Recipe:
    """Insert a recipe via the sync ``create_recipe`` over the async session.

    The repository's create_recipe is synchronous; the test session is
    async (matches the contract that read-side helpers are async).
    ``run_sync`` bridges the two so we can stage rows for the list
    tests below.
    """
    data = _valid_payload(**overrides)
    return await session.run_sync(
        lambda s: create_recipe(s, owner_id=owner_id, data=data)
    )


@pytestmark_pg_required
@pytest.mark.unit
async def test_list_recipes_for_owner_empty_returns_empty_list(
    session: AsyncSession,
) -> None:
    """Owner with zero recipes → empty list (never raises)."""
    owner_id = await _insert_owner(session)

    result = await list_recipes_for_owner(session, owner_id=owner_id)

    assert result == []
    assert isinstance(result, list)


@pytestmark_pg_required
@pytest.mark.unit
async def test_list_recipes_for_owner_returns_single_recipe(
    session: AsyncSession,
) -> None:
    """Owner with one recipe → single-element list."""
    owner_id = await _insert_owner(session)
    created = await _create_recipe_async(
        session, owner_id=owner_id, title="Only"
    )

    result = await list_recipes_for_owner(session, owner_id=owner_id)

    assert len(result) == 1
    assert result[0].id == created.id
    assert result[0].title == "Only"
    assert result[0].owner_id == owner_id


@pytestmark_pg_required
@pytest.mark.unit
async def test_list_recipes_for_owner_orders_by_created_at_desc(
    session: AsyncSession,
) -> None:
    """Three recipes → returned in newest-first order (created_at DESC).

    Matches the compound index BE-001 created on
    ``(owner_id, created_at DESC, id DESC)``.
    """
    owner_id = await _insert_owner(session)
    first = await _create_recipe_async(
        session, owner_id=owner_id, title="First"
    )
    second = await _create_recipe_async(
        session, owner_id=owner_id, title="Second"
    )
    third = await _create_recipe_async(
        session, owner_id=owner_id, title="Third"
    )

    # Each insert uses now() server-default; created_at advances
    # monotonically (or ties, in which case id DESC tiebreaks).
    result = await list_recipes_for_owner(session, owner_id=owner_id)

    assert len(result) == 3
    # Newest first: third was inserted last, so it leads. If two share
    # the exact created_at, id DESC tiebreaks — still deterministic.
    ids = [r.id for r in result]
    assert third.id in ids
    assert second.id in ids
    assert first.id in ids
    # Verify true DESC order on the timestamps that actually came back.
    timestamps = [r.created_at for r in result]
    assert timestamps == sorted(timestamps, reverse=True)


@pytestmark_pg_required
@pytest.mark.unit
async def test_list_recipes_for_owner_tiebreaks_by_id_desc(
    session: AsyncSession,
) -> None:
    """Two rows with identical created_at → id DESC tiebreaks.

    Forces a created_at tie by overwriting timestamps after insert,
    then asserts the returned order is id DESC.
    """
    owner_id = await _insert_owner(session)
    a = await _create_recipe_async(
        session, owner_id=owner_id, title="A"
    )
    b = await _create_recipe_async(
        session, owner_id=owner_id, title="B"
    )

    # Pin both rows to the same created_at to force the tie.
    shared_ts = a.created_at
    await session.execute(
        sa.update(Recipe)
        .where(Recipe.id.in_([a.id, b.id]))
        .values(created_at=shared_ts)
    )
    await session.flush()

    result = await list_recipes_for_owner(session, owner_id=owner_id)

    assert len(result) == 2
    # id DESC tiebreak: the larger UUID comes first.
    expected_first, expected_second = sorted(
        [a, b], key=lambda r: r.id, reverse=True
    )
    assert result[0].id == expected_first.id
    assert result[1].id == expected_second.id


@pytestmark_pg_required
@pytest.mark.unit
async def test_list_recipes_for_owner_filters_by_owner(
    session: AsyncSession,
) -> None:
    """Two owners with recipes → each owner sees only their own.

    Pins the ownership scoping contract: admin moderation across
    users does NOT happen at this layer — filtering is in the WHERE
    clause.
    """
    owner_a = await _insert_owner(session)
    owner_b = await _insert_owner(session)
    a_recipe = await _create_recipe_async(
        session, owner_id=owner_a, title="A's recipe"
    )
    b_recipe = await _create_recipe_async(
        session, owner_id=owner_b, title="B's recipe"
    )

    a_list = await list_recipes_for_owner(session, owner_id=owner_a)
    b_list = await list_recipes_for_owner(session, owner_id=owner_b)

    assert [r.id for r in a_list] == [a_recipe.id]
    assert [r.id for r in b_list] == [b_recipe.id]
    # No cross-bleed in either direction.
    assert b_recipe.id not in [r.id for r in a_list]
    assert a_recipe.id not in [r.id for r in b_list]


@pytestmark_pg_required
@pytest.mark.unit
async def test_list_recipes_for_owner_unknown_owner_returns_empty(
    session: AsyncSession,
) -> None:
    """A UUID with no matching rows → empty list (never raises).

    Even when no users row exists for that id — list_recipes_for_owner
    does not join through users, it just filters Recipe.owner_id.
    """
    # A random UUID that was never inserted into users or recipes.
    random_owner = uuid.uuid4()

    result = await list_recipes_for_owner(session, owner_id=random_owner)

    assert result == []


@pytest.mark.unit
async def test_list_recipes_for_owner_db_exceptions_bubble() -> None:
    """A DB failure on execute propagates unwrapped.

    Mirrors the create_recipe contract: connection drops / timeouts
    bubble untouched so the route layer can translate to 503.
    """
    owner_id = uuid.uuid4()
    boom = OperationalError("stmt", {}, Exception("connection lost"))

    session = MagicMock()

    async def _execute(*args, **kwargs):
        raise boom

    session.execute = _execute

    with pytest.raises(OperationalError) as exc_info:
        await list_recipes_for_owner(session, owner_id=owner_id)

    assert exc_info.value is boom


# --- get_recipe_for_owner ---------------------------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_get_recipe_for_owner_returns_row_when_owned(
    session: AsyncSession,
) -> None:
    """Owner fetching their own recipe → the Recipe row.

    Happy path: a row exists, owner_id matches the lookup, so the
    combined WHERE clause yields the row.
    """
    owner_id = await _insert_owner(session)
    created = await _create_recipe_async(
        session, owner_id=owner_id, title="Owned"
    )

    result = await get_recipe_for_owner(
        session, recipe_id=created.id, owner_id=owner_id
    )

    assert result is not None
    assert result.id == created.id
    assert result.owner_id == owner_id
    assert result.title == "Owned"


@pytestmark_pg_required
@pytest.mark.unit
async def test_get_recipe_for_owner_returns_none_when_absent(
    session: AsyncSession,
) -> None:
    """No recipe with that id → None (not raise).

    Route layer maps this to 404. The owner_id is valid (the user
    row exists), but no recipes row matches the random id.
    """
    owner_id = await _insert_owner(session)
    missing_id = uuid.uuid4()

    result = await get_recipe_for_owner(
        session, recipe_id=missing_id, owner_id=owner_id
    )

    assert result is None


@pytestmark_pg_required
@pytest.mark.unit
async def test_get_recipe_for_owner_returns_none_when_wrong_owner(
    session: AsyncSession,
) -> None:
    """Recipe exists but owned by someone else → None.

    Pins the existence-leak guard: the combined WHERE collapses
    "absent" and "wrong owner" to a single None. A user who guesses
    another owner's recipe id MUST see the same result as a true
    absent-row case.
    """
    owner_a = await _insert_owner(session)
    owner_b = await _insert_owner(session)
    a_recipe = await _create_recipe_async(
        session, owner_id=owner_a, title="A's recipe"
    )

    result = await get_recipe_for_owner(
        session, recipe_id=a_recipe.id, owner_id=owner_b
    )

    assert result is None


@pytestmark_pg_required
@pytest.mark.unit
async def test_get_recipe_for_owner_does_not_leak_across_owners(
    session: AsyncSession,
) -> None:
    """Two owners with recipes each → each only fetches their own row.

    Stronger version of the wrong-owner test: confirms that a valid
    recipe id and a valid owner_id together STILL return None when
    the pair doesn't match. Owner A can fetch A's recipe; owner B
    cannot fetch A's recipe (and vice versa).
    """
    owner_a = await _insert_owner(session)
    owner_b = await _insert_owner(session)
    a_recipe = await _create_recipe_async(
        session, owner_id=owner_a, title="A"
    )
    b_recipe = await _create_recipe_async(
        session, owner_id=owner_b, title="B"
    )

    # Each owner fetches their own recipe — both succeed.
    assert (
        await get_recipe_for_owner(
            session, recipe_id=a_recipe.id, owner_id=owner_a
        )
    ).id == a_recipe.id
    assert (
        await get_recipe_for_owner(
            session, recipe_id=b_recipe.id, owner_id=owner_b
        )
    ).id == b_recipe.id

    # Cross-owner lookups: None in both directions.
    assert (
        await get_recipe_for_owner(
            session, recipe_id=a_recipe.id, owner_id=owner_b
        )
        is None
    )
    assert (
        await get_recipe_for_owner(
            session, recipe_id=b_recipe.id, owner_id=owner_a
        )
        is None
    )


@pytestmark_pg_required
@pytest.mark.unit
async def test_get_recipe_for_owner_unknown_owner_id(
    session: AsyncSession,
) -> None:
    """Owner id with no matching users row → None.

    The function does not join through users, so even an owner_id
    that never existed yields None rather than raising.
    """
    random_owner = uuid.uuid4()
    random_recipe = uuid.uuid4()

    result = await get_recipe_for_owner(
        session, recipe_id=random_recipe, owner_id=random_owner
    )

    assert result is None


@pytest.mark.unit
async def test_get_recipe_for_owner_db_exceptions_bubble() -> None:
    """A DB failure on execute propagates unwrapped.

    Mirrors the list_recipes_for_owner contract: connection drops /
    timeouts bubble untouched so the route layer can translate to 503.
    """
    recipe_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    boom = OperationalError("stmt", {}, Exception("connection lost"))

    session = MagicMock()

    async def _execute(*args, **kwargs):
        raise boom

    session.execute = _execute

    with pytest.raises(OperationalError) as exc_info:
        await get_recipe_for_owner(
            session, recipe_id=recipe_id, owner_id=owner_id
        )

    assert exc_info.value is boom


# --- delete_recipe_for_owner ------------------------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_delete_recipe_for_owner_removes_row_and_returns_true(
    session: AsyncSession,
) -> None:
    """Owner deletes their own recipe → True; row gone from DB.

    Happy path: the combined WHERE matches, DELETE removes exactly one
    row, ``result.rowcount`` is 1, function returns True, and a
    subsequent SELECT can no longer find the row in the same
    transaction.
    """
    owner_id = await _insert_owner(session)
    created = await _create_recipe_async(
        session, owner_id=owner_id, title="To be deleted"
    )

    deleted = await session.run_sync(
        lambda s: delete_recipe_for_owner(
            s, recipe_id=created.id, owner_id=owner_id
        )
    )

    assert deleted is True

    # Verify the row really is gone in the same transaction.
    fetched = await session.execute(
        sa.select(Recipe).where(Recipe.id == created.id)
    )
    assert fetched.scalar_one_or_none() is None


@pytestmark_pg_required
@pytest.mark.unit
async def test_delete_recipe_for_owner_absent_returns_false(
    session: AsyncSession,
) -> None:
    """No recipe with that id → False (not raise).

    Route layer maps this to 404. The owner_id is valid (the users
    row exists), but no recipes row matches the random id, so
    ``rowcount`` is 0 and the function returns False.
    """
    owner_id = await _insert_owner(session)
    missing_id = uuid.uuid4()

    deleted = await session.run_sync(
        lambda s: delete_recipe_for_owner(
            s, recipe_id=missing_id, owner_id=owner_id
        )
    )

    assert deleted is False


@pytestmark_pg_required
@pytest.mark.unit
async def test_delete_recipe_for_owner_wrong_owner_returns_false(
    session: AsyncSession,
) -> None:
    """Recipe exists but owned by someone else → False; row untouched.

    Pins the existence-leak guard: the combined WHERE collapses
    "absent" and "wrong owner" to a single False return. Verifies the
    untouched row remains in the DB so owner A's recipe is NOT
    deleted by owner B's attempt.
    """
    owner_a = await _insert_owner(session)
    owner_b = await _insert_owner(session)
    a_recipe = await _create_recipe_async(
        session, owner_id=owner_a, title="A's recipe"
    )

    deleted = await session.run_sync(
        lambda s: delete_recipe_for_owner(
            s, recipe_id=a_recipe.id, owner_id=owner_b
        )
    )

    assert deleted is False

    # A's recipe must still be present in the DB.
    fetched = await session.execute(
        sa.select(Recipe).where(Recipe.id == a_recipe.id)
    )
    row = fetched.scalar_one()
    assert row.owner_id == owner_a


@pytestmark_pg_required
@pytest.mark.unit
async def test_delete_recipe_for_owner_does_not_affect_other_recipes(
    session: AsyncSession,
) -> None:
    """Deleting one recipe leaves the owner's other recipes intact.

    Pins the WHERE clause scopes to a single id — a DELETE without
    the id filter would wipe all of the owner's recipes. This test
    catches that regression.
    """
    owner_id = await _insert_owner(session)
    keep = await _create_recipe_async(
        session, owner_id=owner_id, title="Keep me"
    )
    drop = await _create_recipe_async(
        session, owner_id=owner_id, title="Drop me"
    )

    deleted = await session.run_sync(
        lambda s: delete_recipe_for_owner(
            s, recipe_id=drop.id, owner_id=owner_id
        )
    )

    assert deleted is True

    # Surviving recipe is still listable by the owner.
    remaining = await list_recipes_for_owner(session, owner_id=owner_id)
    assert [r.id for r in remaining] == [keep.id]


@pytestmark_pg_required
@pytest.mark.unit
async def test_delete_recipe_for_owner_double_delete_returns_false(
    session: AsyncSession,
) -> None:
    """First delete returns True; a second delete on the same id returns False.

    Idempotency boundary from the delete_recipe capability: a second
    DELETE on the same id by the same owner returns False (the
    recipe is genuinely gone). Route layer translates the second
    call to 404.
    """
    owner_id = await _insert_owner(session)
    created = await _create_recipe_async(
        session, owner_id=owner_id, title="Once"
    )

    first = await session.run_sync(
        lambda s: delete_recipe_for_owner(
            s, recipe_id=created.id, owner_id=owner_id
        )
    )
    second = await session.run_sync(
        lambda s: delete_recipe_for_owner(
            s, recipe_id=created.id, owner_id=owner_id
        )
    )

    assert first is True
    assert second is False


@pytest.mark.unit
def test_delete_recipe_for_owner_db_exceptions_bubble() -> None:
    """A DB failure (OperationalError) on execute propagates unwrapped.

    Mirrors the create_recipe contract: connection drops / timeouts
    bubble untouched so the route layer can translate them to 503.
    Repository does NOT call rollback — transaction state belongs to
    the caller (the route layer).
    """
    recipe_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    boom = OperationalError("stmt", {}, Exception("connection lost"))

    session = MagicMock()
    session.execute.side_effect = boom

    with pytest.raises(OperationalError) as exc_info:
        delete_recipe_for_owner(
            session, recipe_id=recipe_id, owner_id=owner_id
        )

    assert exc_info.value is boom
    session.rollback.assert_not_called()


@pytest.mark.unit
def test_delete_recipe_for_owner_does_not_commit() -> None:
    """The function flushes (not commits) — route layer owns the txn.

    Same convention as :func:`create_recipe` and
    :func:`app.repositories.user_repository.upsert_user_mirror`.
    Asserts the repository does NOT call ``session.commit`` so the
    route layer remains the single transaction-boundary owner.
    """
    recipe_id = uuid.uuid4()
    owner_id = uuid.uuid4()

    session = MagicMock()
    # rowcount=0 keeps the return value False; this test cares about
    # the commit/flush contract, not the return value.
    session.execute.return_value.rowcount = 0

    result = delete_recipe_for_owner(
        session, recipe_id=recipe_id, owner_id=owner_id
    )

    assert result is False
    session.flush.assert_called_once()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


# --- update_recipe_for_owner ------------------------------------------


@pytestmark_pg_required
@pytest.mark.unit
async def test_update_recipe_for_owner_updates_row_and_returns_it(
    session: AsyncSession,
) -> None:
    """Happy path: owner updates → returned Recipe carries the new field values.

    The combined ``id = :recipe_id AND owner_id = :owner_id`` WHERE
    clause matches, the SET clause overwrites every editable field
    from ``data.model_dump()``, and ``updated_at`` advances to now().
    """
    owner_id = await _insert_owner(session)
    created = await _create_recipe_async(
        session, owner_id=owner_id, title="Original"
    )
    new_data = _valid_payload(
        title="Updated",
        description="Updated description for the recipe.",
        ingredients=["new", "ingredients"],
        instructions=["new step 1", "new step 2"],
        prep_time=30,
        cook_time=45,
        servings=8,
    )

    updated = await session.run_sync(
        lambda s: update_recipe_for_owner(
            s, recipe_id=created.id, owner_id=owner_id, data=new_data
        )
    )

    assert updated is not None
    assert updated.id == created.id
    assert updated.owner_id == owner_id
    assert updated.title == "Updated"
    assert updated.description == new_data.description
    assert updated.ingredients == new_data.ingredients
    assert updated.instructions == new_data.instructions
    assert updated.prep_time == 30
    assert updated.cook_time == 45
    assert updated.servings == 8


@pytestmark_pg_required
@pytest.mark.unit
async def test_update_recipe_for_owner_persists_in_db(
    session: AsyncSession,
) -> None:
    """A subsequent SELECT in the same transaction finds the mutated row.

    Verifies the function actually writes to the session (not just
    constructs a transient ORM instance).
    """
    owner_id = await _insert_owner(session)
    created = await _create_recipe_async(
        session, owner_id=owner_id, title="Before"
    )
    new_data = _valid_payload(title="After")

    await session.run_sync(
        lambda s: update_recipe_for_owner(
            s, recipe_id=created.id, owner_id=owner_id, data=new_data
        )
    )

    fetched = await session.execute(
        sa.select(Recipe).where(Recipe.id == created.id)
    )
    row = fetched.scalar_one()
    assert row.id == created.id
    assert row.title == "After"
    assert row.owner_id == owner_id


@pytestmark_pg_required
@pytest.mark.unit
async def test_update_recipe_for_owner_absent_returns_none(
    session: AsyncSession,
) -> None:
    """No recipe with that id → None (not raise).

    Route layer maps this to 404. The owner_id is valid but no recipes
    row matches the random id, so RETURNING produces no rows.
    """
    owner_id = await _insert_owner(session)
    missing_id = uuid.uuid4()
    new_data = _valid_payload()

    result = await session.run_sync(
        lambda s: update_recipe_for_owner(
            s, recipe_id=missing_id, owner_id=owner_id, data=new_data
        )
    )

    assert result is None


@pytestmark_pg_required
@pytest.mark.unit
async def test_update_recipe_for_owner_wrong_owner_returns_none(
    session: AsyncSession,
) -> None:
    """Recipe exists but owned by someone else → None; row untouched.

    Pins the existence-leak guard: the combined WHERE collapses
    "absent" and "wrong owner" to a single None return. Verifies the
    untouched row remains in the DB so owner A's recipe is NOT
    mutated by owner B's attempt.
    """
    owner_a = await _insert_owner(session)
    owner_b = await _insert_owner(session)
    a_recipe = await _create_recipe_async(
        session, owner_id=owner_a, title="A's recipe"
    )
    new_data = _valid_payload(title="B's attempt")

    result = await session.run_sync(
        lambda s: update_recipe_for_owner(
            s, recipe_id=a_recipe.id, owner_id=owner_b, data=new_data
        )
    )

    assert result is None

    # A's recipe must still carry the original title — no partial write.
    fetched = await session.execute(
        sa.select(Recipe).where(Recipe.id == a_recipe.id)
    )
    row = fetched.scalar_one()
    assert row.owner_id == owner_a
    assert row.title == "A's recipe"


@pytestmark_pg_required
@pytest.mark.unit
async def test_update_recipe_for_owner_preserves_immutable_columns(
    session: AsyncSession,
) -> None:
    """Update never mutates ``id``, ``owner_id``, or ``created_at``.

    Pins the contract that the SET clause only names editable fields
    (sourced from ``data.model_dump()`` plus the server-side
    ``updated_at``). The three immutable columns must match the
    pre-update snapshot byte-for-byte.
    """
    owner_id = await _insert_owner(session)
    created = await _create_recipe_async(
        session, owner_id=owner_id, title="Pre-update"
    )
    original_id = created.id
    original_owner_id = created.owner_id
    original_created_at = created.created_at

    updated = await session.run_sync(
        lambda s: update_recipe_for_owner(
            s,
            recipe_id=created.id,
            owner_id=owner_id,
            data=_valid_payload(title="Post-update"),
        )
    )

    assert updated is not None
    assert updated.id == original_id
    assert updated.owner_id == original_owner_id
    assert updated.created_at == original_created_at


@pytestmark_pg_required
@pytest.mark.unit
async def test_update_recipe_for_owner_advances_updated_at(
    session: AsyncSession,
) -> None:
    """``updated_at`` advances to a value >= ``created_at`` after update.

    Mirrors the contract's ``updated_at_advances`` scenario. The
    function sets ``updated_at = func.now()`` explicitly, so the new
    timestamp must be >= the original ``created_at`` (and typically
    strictly greater, modulo clock resolution).
    """
    owner_id = await _insert_owner(session)
    created = await _create_recipe_async(
        session, owner_id=owner_id, title="Original"
    )
    original_updated_at = created.updated_at

    updated = await session.run_sync(
        lambda s: update_recipe_for_owner(
            s,
            recipe_id=created.id,
            owner_id=owner_id,
            data=_valid_payload(title="Updated"),
        )
    )

    assert updated is not None
    # updated_at must not regress; with func.now() it is >= the
    # original (clock-resolution ties are tolerated).
    assert updated.updated_at >= original_updated_at
    assert updated.updated_at >= updated.created_at


@pytestmark_pg_required
@pytest.mark.unit
async def test_update_recipe_for_owner_replaces_ingredients_not_merges(
    session: AsyncSession,
) -> None:
    """Full-replacement semantics: a smaller ingredients list overwrites the prior.

    Matches the PUT contract's ``ingredients_replaced_not_merged``
    scenario: the SET clause assigns the new value verbatim, so prior
    items are gone after the update.
    """
    owner_id = await _insert_owner(session)
    created = await _create_recipe_async(
        session,
        owner_id=owner_id,
        ingredients=["flour", "water", "salt"],
    )

    updated = await session.run_sync(
        lambda s: update_recipe_for_owner(
            s,
            recipe_id=created.id,
            owner_id=owner_id,
            data=_valid_payload(ingredients=["sugar"]),
        )
    )

    assert updated is not None
    assert updated.ingredients == ["sugar"]


@pytestmark_pg_required
@pytest.mark.unit
async def test_update_recipe_for_owner_preserves_unicode(
    session: AsyncSession,
) -> None:
    """Unicode in updated fields round-trips byte-identical.

    Matches the PUT contract's ``unicode_round_trip`` scenario.
    """
    owner_id = await _insert_owner(session)
    created = await _create_recipe_async(
        session, owner_id=owner_id, title="Pre"
    )

    updated = await session.run_sync(
        lambda s: update_recipe_for_owner(
            s,
            recipe_id=created.id,
            owner_id=owner_id,
            data=_valid_payload(
                title="Soupe à l'oignon 🧅",
                description="French onion soup — 美味しい",
                ingredients=["oignons 🧅", "fromage 🧀"],
                instructions=["étape 1", "étape 2"],
            ),
        )
    )

    assert updated is not None
    assert updated.title == "Soupe à l'oignon 🧅"
    assert updated.description == "French onion soup — 美味しい"
    assert updated.ingredients == ["oignons 🧅", "fromage 🧀"]
    assert updated.instructions == ["étape 1", "étape 2"]


@pytest.mark.unit
def test_update_recipe_for_owner_db_exceptions_bubble() -> None:
    """A DB failure (OperationalError) on execute propagates unwrapped.

    Mirrors the create_recipe / delete_recipe_for_owner contract:
    connection drops / timeouts bubble untouched so the route layer
    can translate them to 503. Repository does NOT call rollback —
    transaction state belongs to the caller.
    """
    recipe_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    data = _valid_payload()
    boom = OperationalError("stmt", {}, Exception("connection lost"))

    session = MagicMock()
    session.execute.side_effect = boom

    with pytest.raises(OperationalError) as exc_info:
        update_recipe_for_owner(
            session, recipe_id=recipe_id, owner_id=owner_id, data=data
        )

    assert exc_info.value is boom
    session.rollback.assert_not_called()


@pytest.mark.unit
def test_update_recipe_for_owner_does_not_commit() -> None:
    """The function flushes (not commits) — route layer owns the txn.

    Same convention as :func:`create_recipe`,
    :func:`delete_recipe_for_owner`, and
    :func:`app.repositories.user_repository.upsert_user_mirror`.
    """
    recipe_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    data = _valid_payload()

    session = MagicMock()
    # scalar_one_or_none returns None → no row matched; this test
    # cares about commit/flush behavior, not the return value.
    session.execute.return_value.scalar_one_or_none.return_value = None

    result = update_recipe_for_owner(
        session, recipe_id=recipe_id, owner_id=owner_id, data=data
    )

    assert result is None
    session.flush.assert_called_once()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
