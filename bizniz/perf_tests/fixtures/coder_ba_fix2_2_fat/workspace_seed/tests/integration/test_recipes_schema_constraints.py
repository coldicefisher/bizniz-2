"""Integration tests for the recipes table's DB-level schema (BE-001-fix1).

These tests run against the live Postgres database and verify that
the recipes migration applies its DDL (CHECK constraints, FK
CASCADE, compound index) to the live ``recipes`` table. The pre-fix
bug was that ``Base.metadata.create_all`` ran first and created the
table without CHECK constraints; the migration's ``CREATE TABLE IF
NOT EXISTS`` then no-oped on the already-existing table.
CodeReviewer caught this; the fix flips the order so the migration
is the source of truth for the recipes table's DDL.

Strategy: each test connects to the live database via the
``DATABASE_URL`` environment variable. A function-scoped engine
fixture creates a fresh ``AsyncEngine`` per test (asyncpg pins
connections to the event loop they were created on, so module-
scoped engines explode with "attached to a different loop" under
pytest-asyncio function-scope loops).

A second function-scoped autouse fixture ensures the ``recipes``
table exists with constraints before each test by dropping any
pre-fix unconstrained table and re-running the migration. The
migration's ``IF NOT EXISTS`` clauses make repeated runs cheap.

Skipped (with a clear marker) if ``DATABASE_URL`` is unset, so unit-
only CI environments aren't forced to provision Postgres.
"""
from __future__ import annotations

import os
import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.db.base import Base
from app.db.migrations import recipes as recipes_migration


def _live_database_url() -> str | None:
    """Return the live async ``DATABASE_URL`` or ``None`` if unset."""
    return os.environ.get("DATABASE_URL")


@pytest_asyncio.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    """Async SQLAlchemy engine bound to the live Postgres database.

    Function-scoped: asyncpg pins connections to the event loop they
    were created on, so a module-scoped engine fails the second test
    with "attached to a different loop." Per-test engine creation
    costs ~50ms, which is acceptable.
    """
    url = _live_database_url()
    if not url:
        pytest.skip("DATABASE_URL not set; skipping live-DB schema test")
    engine = create_async_engine(url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _reset_recipes_schema(async_engine: AsyncEngine) -> None:
    """Drop and re-run the recipes migration before each test.

    The pre-fix live DB has a ``recipes`` table created by
    ``Base.metadata.create_all`` without CHECK constraints. To
    verify the fix's contract (that the migration produces a fully-
    constrained table) we drop the table first, then run the
    migration. The migration is idempotent so re-runs across tests
    are safe.

    The fixture also ensures ``users`` exists so the recipes
    migration's FK target resolves.
    """
    async with async_engine.begin() as conn:
        if "users" in Base.metadata.tables:
            users_table = Base.metadata.tables["users"]

            def _create_users(sync_conn) -> None:
                Base.metadata.create_all(sync_conn, tables=[users_table])

            await conn.run_sync(_create_users)
        await conn.execute(text("DROP TABLE IF EXISTS recipes"))
        await conn.run_sync(recipes_migration.run_migration)


async def _check_constraint_defs(conn: AsyncConnection) -> list[str]:
    """Return the ``pg_get_constraintdef`` text of every CHECK on recipes."""
    result = await conn.execute(
        text(
            "SELECT pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c "
            "JOIN pg_class t ON c.conrelid = t.oid "
            "WHERE t.relname = 'recipes' AND c.contype = 'c'"
        )
    )
    rows = result.fetchall()
    return [r[0].lower() for r in rows]


async def test_pgcrypto_extension_is_available(async_engine: AsyncEngine) -> None:
    """``gen_random_uuid()`` is callable after startup (pgcrypto installed)."""
    async with async_engine.connect() as conn:
        result = await conn.execute(text("SELECT gen_random_uuid()"))
        value = result.scalar()
    assert isinstance(value, uuid.UUID), (
        f"gen_random_uuid() should return a uuid; got {value!r}"
    )


def _has_range_check(defs: list[str], column: str, lo: int, hi: int) -> bool:
    """True iff some CHECK on ``column`` bounds it in ``[lo, hi]``.

    Postgres normalizes ``BETWEEN x AND y`` from the source DDL to
    ``(col >= x) AND (col <= y)`` in ``pg_get_constraintdef``, so
    the assertion looks for both the lower bound and the upper
    bound substrings rather than the literal ``BETWEEN`` keyword.
    """
    lo_pat = f">= {lo}"
    hi_pat = f"<= {hi}"
    return any(column in d and lo_pat in d and hi_pat in d for d in defs)


async def test_recipes_table_has_title_length_check(
    async_engine: AsyncEngine,
) -> None:
    """The CHECK on ``title`` enforces trimmed length 1..200."""
    async with async_engine.connect() as conn:
        defs = await _check_constraint_defs(conn)
    assert _has_range_check(defs, "length(trim(", 1, 200) and any(
        "title" in d for d in defs
    ), (
        f"expected CHECK on length(trim(title)) BETWEEN 1 AND 200; "
        f"got: {defs!r}"
    )
    assert any(
        "title" in d and ">= 1" in d and "<= 200" in d for d in defs
    ), (
        f"expected CHECK on title length 1..200; got: {defs!r}"
    )


async def test_recipes_table_has_description_length_check(
    async_engine: AsyncEngine,
) -> None:
    """The CHECK on ``description`` enforces trimmed length 1..5000."""
    async with async_engine.connect() as conn:
        defs = await _check_constraint_defs(conn)
    assert any(
        "description" in d and ">= 1" in d and "<= 5000" in d for d in defs
    ), (
        f"expected CHECK on length(trim(description)) BETWEEN 1 AND 5000; "
        f"got: {defs!r}"
    )


async def test_recipes_table_has_prep_time_check(
    async_engine: AsyncEngine,
) -> None:
    """The CHECK on ``prep_time`` enforces 0..1440 minutes."""
    async with async_engine.connect() as conn:
        defs = await _check_constraint_defs(conn)
    assert _has_range_check(defs, "prep_time", 0, 1440), (
        f"expected CHECK on prep_time BETWEEN 0 AND 1440; got: {defs!r}"
    )


async def test_recipes_table_has_cook_time_check(
    async_engine: AsyncEngine,
) -> None:
    """The CHECK on ``cook_time`` enforces 0..1440 minutes."""
    async with async_engine.connect() as conn:
        defs = await _check_constraint_defs(conn)
    assert _has_range_check(defs, "cook_time", 0, 1440), (
        f"expected CHECK on cook_time BETWEEN 0 AND 1440; got: {defs!r}"
    )


async def test_recipes_table_has_servings_check(
    async_engine: AsyncEngine,
) -> None:
    """The CHECK on ``servings`` enforces 1..1000."""
    async with async_engine.connect() as conn:
        defs = await _check_constraint_defs(conn)
    assert _has_range_check(defs, "servings", 1, 1000), (
        f"expected CHECK on servings BETWEEN 1 AND 1000; got: {defs!r}"
    )


async def _seed_user(conn: AsyncConnection) -> uuid.UUID:
    """Insert a throwaway user and return its id. Caller controls the tx."""
    user_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO users (id, email, role, display_name) "
            "VALUES (:id, :email, 'user', 'Schema Test User')"
        ),
        {"id": user_id, "email": f"schema-{user_id}@example.com"},
    )
    return user_id


def _insert_recipe_sql() -> str:
    """Direct SQL for INSERT INTO recipes — bypasses the API layer.

    The whole point of the DB CHECK constraints is defense-in-depth
    against bypassed-API writes. So we go straight at the table.
    """
    return (
        "INSERT INTO recipes "
        "(owner_id, title, description, ingredients, instructions, "
        "prep_time, cook_time, servings) "
        "VALUES (:owner_id, :title, :description, "
        "CAST(:ingredients AS jsonb), CAST(:instructions AS jsonb), "
        ":prep_time, :cook_time, :servings)"
    )


def _valid_recipe_payload(owner_id: uuid.UUID) -> dict:
    return {
        "owner_id": owner_id,
        "title": "Valid Title",
        "description": "Valid description with enough chars.",
        "ingredients": '["1 cup flour", "2 eggs"]',
        "instructions": '["Mix", "Bake"]',
        "prep_time": 10,
        "cook_time": 20,
        "servings": 4,
    }


async def test_insert_with_zero_servings_is_rejected(
    async_engine: AsyncEngine,
) -> None:
    """Direct INSERT with ``servings=0`` is rejected by the CHECK."""
    async with async_engine.connect() as conn:
        trans = await conn.begin()
        try:
            owner_id = await _seed_user(conn)
            payload = _valid_recipe_payload(owner_id)
            payload["servings"] = 0
            with pytest.raises(IntegrityError) as excinfo:
                await conn.execute(text(_insert_recipe_sql()), payload)
            assert "check" in str(excinfo.value).lower(), (
                f"expected CHECK violation in error; got: {excinfo.value!r}"
            )
        finally:
            await trans.rollback()


async def test_insert_with_negative_prep_time_is_rejected(
    async_engine: AsyncEngine,
) -> None:
    """Direct INSERT with ``prep_time=-1`` is rejected by the CHECK."""
    async with async_engine.connect() as conn:
        trans = await conn.begin()
        try:
            owner_id = await _seed_user(conn)
            payload = _valid_recipe_payload(owner_id)
            payload["prep_time"] = -1
            with pytest.raises(IntegrityError) as excinfo:
                await conn.execute(text(_insert_recipe_sql()), payload)
            assert "check" in str(excinfo.value).lower(), (
                f"expected CHECK violation in error; got: {excinfo.value!r}"
            )
        finally:
            await trans.rollback()


async def test_insert_with_empty_title_is_rejected(
    async_engine: AsyncEngine,
) -> None:
    """Direct INSERT with ``title=''`` (trim length 0) is rejected by CHECK."""
    async with async_engine.connect() as conn:
        trans = await conn.begin()
        try:
            owner_id = await _seed_user(conn)
            payload = _valid_recipe_payload(owner_id)
            payload["title"] = ""
            with pytest.raises(IntegrityError) as excinfo:
                await conn.execute(text(_insert_recipe_sql()), payload)
            assert "check" in str(excinfo.value).lower(), (
                f"expected CHECK violation in error; got: {excinfo.value!r}"
            )
        finally:
            await trans.rollback()


async def test_valid_insert_succeeds(async_engine: AsyncEngine) -> None:
    """A within-bounds INSERT is accepted — sanity check on the constraints."""
    async with async_engine.connect() as conn:
        trans = await conn.begin()
        try:
            owner_id = await _seed_user(conn)
            payload = _valid_recipe_payload(owner_id)
            result = await conn.execute(
                text(_insert_recipe_sql() + " RETURNING id"), payload
            )
            new_id = result.scalar()
            assert new_id is not None, "INSERT RETURNING id produced no row"
        finally:
            await trans.rollback()


async def test_fk_cascade_deletes_recipes_when_user_deleted(
    async_engine: AsyncEngine,
) -> None:
    """Deleting a user cascades to delete that user's recipes."""
    async with async_engine.connect() as conn:
        trans = await conn.begin()
        try:
            owner_id = await _seed_user(conn)
            await conn.execute(
                text(_insert_recipe_sql()), _valid_recipe_payload(owner_id)
            )

            before_result = await conn.execute(
                text("SELECT count(*) FROM recipes WHERE owner_id = :id"),
                {"id": owner_id},
            )
            before = before_result.scalar()
            assert before == 1, f"expected 1 recipe after INSERT; got {before}"

            await conn.execute(
                text("DELETE FROM users WHERE id = :id"), {"id": owner_id}
            )

            after_result = await conn.execute(
                text("SELECT count(*) FROM recipes WHERE owner_id = :id"),
                {"id": owner_id},
            )
            after = after_result.scalar()
            assert after == 0, (
                f"expected recipes to cascade-delete with the user; "
                f"got {after} remaining"
            )
        finally:
            await trans.rollback()


async def test_compound_index_exists(async_engine: AsyncEngine) -> None:
    """The compound sort index is present on the live table."""
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'recipes' "
                "AND indexname = 'ix_recipes_owner_created_id'"
            )
        )
        row = result.fetchone()
    assert row is not None, "ix_recipes_owner_created_id index is missing"
    indexdef = row[0].lower()
    assert "owner_id" in indexdef
    assert "created_at" in indexdef
    assert "id" in indexdef


async def test_fk_to_users_has_on_delete_cascade(
    async_engine: AsyncEngine,
) -> None:
    """The FK from ``recipes.owner_id`` is declared ``ON DELETE CASCADE``."""
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON c.conrelid = t.oid "
                "WHERE t.relname = 'recipes' AND c.contype = 'f'"
            )
        )
        rows = result.fetchall()
    defs = [r[0].lower() for r in rows]
    assert any(
        "references users(id)" in d and "on delete cascade" in d for d in defs
    ), (
        f"expected FK to users(id) ON DELETE CASCADE; got: {defs!r}"
    )


async def test_migration_is_idempotent(async_engine: AsyncEngine) -> None:
    """Running the recipes migration a second time does not raise."""
    async with async_engine.begin() as conn:
        await conn.run_sync(recipes_migration.run_migration)
        await conn.run_sync(recipes_migration.run_migration)


async def test_recipes_table_has_exactly_five_check_constraints(
    async_engine: AsyncEngine,
) -> None:
    """Exactly the five spec CHECK constraints are present on recipes.

    Locks the count so a future schema change that drops one of the
    constraints (regression of the BE-001 bug) fails this assertion
    immediately. Five checks: title length, description length,
    prep_time, cook_time, servings.
    """
    async with async_engine.connect() as conn:
        defs = await _check_constraint_defs(conn)
    assert len(defs) == 5, (
        f"expected exactly 5 CHECK constraints on recipes; got "
        f"{len(defs)}: {defs!r}"
    )
