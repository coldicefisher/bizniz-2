"""Unit tests for the recipes table migration module.

These tests verify the migration's *intent* by passing a mock
``Connection`` to ``run_migration`` and inspecting the SQL strings
that flow through ``connection.execute(...)``. They do not connect to
Postgres — live-DB behavior is covered downstream by the startup
migration runner (BE-001-U3) and integration tests in later
milestones.

What's enforced here:

- ``run_migration`` is callable with a single ``connection`` arg.
- The ``pgcrypto`` extension is created BEFORE the ``recipes`` table
  (the table's ``DEFAULT gen_random_uuid()`` resolves only after the
  extension exists).
- The CREATE TABLE statement is idempotent — uses ``IF NOT EXISTS``.
- Every column in the issue spec is present with the right type.
- Every CHECK constraint in the issue spec is encoded.
- The FK from ``owner_id`` to ``users(id)`` is declared with ``ON
  DELETE CASCADE`` so user removal sweeps recipes.
- The compound index ``ix_recipes_owner_created_id`` on
  ``(owner_id, created_at DESC, id DESC)`` is created AFTER the table
  with a stable name and ``IF NOT EXISTS`` for idempotent re-runs.
- FK creation errors propagate — no try/except around the DDL.
"""
from __future__ import annotations

import re
from unittest import mock

import pytest

from app.db.migrations import recipes as recipes_migration


@pytest.fixture
def mock_connection() -> mock.MagicMock:
    """Mock SQLAlchemy ``Connection`` capturing ``execute`` calls."""
    return mock.MagicMock()


def _executed_sql(conn: mock.MagicMock) -> list[str]:
    """Return the SQL string of each ``connection.execute(text(...))`` call.

    ``connection.execute`` is called with a ``TextClause`` produced by
    ``sqlalchemy.text(...)``. ``str(TextClause)`` returns the raw SQL
    text the migration passed in.
    """
    statements: list[str] = []
    for call in conn.execute.call_args_list:
        # First positional arg is the TextClause.
        statements.append(str(call.args[0]))
    return statements


def _normalize(sql: str) -> str:
    """Collapse whitespace + lowercase so substring assertions are robust."""
    return re.sub(r"\s+", " ", sql).strip().lower()


def test_module_exposes_run_migration() -> None:
    """``run_migration`` is exported and callable from the module."""
    assert callable(recipes_migration.run_migration)


def test_run_migration_executes_three_ddl_statements(
    mock_connection: mock.MagicMock,
) -> None:
    """``run_migration`` issues three DDL statements: extension + table + index."""
    recipes_migration.run_migration(mock_connection)
    assert mock_connection.execute.call_count == 3, (
        f"expected 3 execute() calls (extension + table + index), got "
        f"{mock_connection.execute.call_count}: {_executed_sql(mock_connection)!r}"
    )


def test_run_migration_creates_pgcrypto_extension_first(
    mock_connection: mock.MagicMock,
) -> None:
    """``CREATE EXTENSION pgcrypto`` precedes the CREATE TABLE call.

    The ``recipes.id`` default is ``gen_random_uuid()``, which lives in
    the pgcrypto extension. Creating the table before the extension
    raises ``function gen_random_uuid() does not exist``.
    """
    recipes_migration.run_migration(mock_connection)
    stmts = [_normalize(s) for s in _executed_sql(mock_connection)]
    assert stmts, "expected at least one execute() call"
    first = stmts[0]
    assert "create extension" in first
    assert "if not exists" in first
    assert "pgcrypto" in first


def test_run_migration_creates_recipes_table_idempotently(
    mock_connection: mock.MagicMock,
) -> None:
    """The CREATE TABLE statement uses ``IF NOT EXISTS`` for re-runs."""
    recipes_migration.run_migration(mock_connection)
    stmts = [_normalize(s) for s in _executed_sql(mock_connection)]
    table_stmts = [s for s in stmts if "create table" in s]
    assert len(table_stmts) == 1, (
        f"expected exactly one CREATE TABLE, got {len(table_stmts)}: "
        f"{table_stmts!r}"
    )
    table_sql = table_stmts[0]
    assert "if not exists" in table_sql
    assert "recipes" in table_sql


@pytest.mark.parametrize(
    "column_name,type_keyword",
    [
        ("id", "uuid"),
        ("owner_id", "uuid"),
        ("title", "text"),
        ("description", "text"),
        ("ingredients", "jsonb"),
        ("instructions", "jsonb"),
        ("prep_time", "integer"),
        ("cook_time", "integer"),
        ("servings", "integer"),
        ("created_at", "timestamptz"),
        ("updated_at", "timestamptz"),
    ],
)
def test_recipes_table_declares_column(
    mock_connection: mock.MagicMock,
    column_name: str,
    type_keyword: str,
) -> None:
    """Each column in the issue spec appears with the right Postgres type."""
    recipes_migration.run_migration(mock_connection)
    table_sql = next(
        s for s in (_normalize(x) for x in _executed_sql(mock_connection))
        if "create table" in s
    )
    # Column name appears followed (allowing whitespace) by its type.
    pattern = rf"\b{re.escape(column_name)}\s+{re.escape(type_keyword)}\b"
    assert re.search(pattern, table_sql), (
        f"expected column `{column_name} {type_keyword}` in CREATE TABLE; "
        f"got: {table_sql!r}"
    )


def test_id_column_uses_gen_random_uuid_default(
    mock_connection: mock.MagicMock,
) -> None:
    """``id`` defaults to ``gen_random_uuid()`` (server-side, never client-set)."""
    recipes_migration.run_migration(mock_connection)
    table_sql = next(
        s for s in (_normalize(x) for x in _executed_sql(mock_connection))
        if "create table" in s
    )
    assert "gen_random_uuid()" in table_sql
    assert re.search(r"\bid\s+uuid\s+primary\s+key", table_sql), (
        f"expected `id uuid PRIMARY KEY ...`; got: {table_sql!r}"
    )


def test_owner_id_has_fk_to_users_with_cascade(
    mock_connection: mock.MagicMock,
) -> None:
    """``owner_id`` is FK to ``users(id)`` with ``ON DELETE CASCADE``.

    Cascade is load-bearing: deleting a user must sweep their recipes
    rather than leave orphans pinned by a NOT NULL FK.
    """
    recipes_migration.run_migration(mock_connection)
    table_sql = next(
        s for s in (_normalize(x) for x in _executed_sql(mock_connection))
        if "create table" in s
    )
    assert "references users(id)" in table_sql
    assert "on delete cascade" in table_sql


@pytest.mark.parametrize(
    "check_fragment",
    [
        # Title / description length bounds — defense-in-depth behind API.
        "length(trim(title)) between 1 and 200",
        "length(trim(description)) between 1 and 5000",
        # Time/quantity bounds from the issue spec.
        "prep_time between 0 and 1440",
        "cook_time between 0 and 1440",
        "servings between 1 and 1000",
    ],
)
def test_table_encodes_check_constraint(
    mock_connection: mock.MagicMock,
    check_fragment: str,
) -> None:
    """Every CHECK constraint from the issue spec is on the table."""
    recipes_migration.run_migration(mock_connection)
    table_sql = next(
        s for s in (_normalize(x) for x in _executed_sql(mock_connection))
        if "create table" in s
    )
    assert check_fragment in table_sql, (
        f"expected CHECK fragment `{check_fragment}` in CREATE TABLE; "
        f"got: {table_sql!r}"
    )


def test_audit_columns_default_to_now(
    mock_connection: mock.MagicMock,
) -> None:
    """``created_at`` and ``updated_at`` have ``DEFAULT now()``."""
    recipes_migration.run_migration(mock_connection)
    table_sql = next(
        s for s in (_normalize(x) for x in _executed_sql(mock_connection))
        if "create table" in s
    )
    assert re.search(r"created_at\s+timestamptz\s+not\s+null\s+default\s+now\(\)", table_sql), (
        f"expected `created_at timestamptz NOT NULL DEFAULT now()`; got: {table_sql!r}"
    )
    assert re.search(r"updated_at\s+timestamptz\s+not\s+null\s+default\s+now\(\)", table_sql), (
        f"expected `updated_at timestamptz NOT NULL DEFAULT now()`; got: {table_sql!r}"
    )


def test_compound_index_created_with_stable_name(
    mock_connection: mock.MagicMock,
) -> None:
    """The compound sort index uses the stable name from the issue spec.

    A stable index name is what makes ``CREATE INDEX IF NOT EXISTS``
    a true no-op on re-runs: Postgres skips creation when an index of
    that name already exists, regardless of definition.
    """
    recipes_migration.run_migration(mock_connection)
    stmts = [_normalize(s) for s in _executed_sql(mock_connection)]
    index_stmts = [s for s in stmts if "create index" in s]
    assert len(index_stmts) == 1, (
        f"expected exactly one CREATE INDEX, got {len(index_stmts)}: "
        f"{index_stmts!r}"
    )
    index_sql = index_stmts[0]
    assert "if not exists" in index_sql, (
        f"index DDL must be idempotent (IF NOT EXISTS); got: {index_sql!r}"
    )
    assert "ix_recipes_owner_created_id" in index_sql, (
        f"index name must be the stable `ix_recipes_owner_created_id`; "
        f"got: {index_sql!r}"
    )
    assert " on recipes " in index_sql, (
        f"index must target the `recipes` table; got: {index_sql!r}"
    )


def test_compound_index_columns_and_sort_order(
    mock_connection: mock.MagicMock,
) -> None:
    """The compound index columns are (owner_id, created_at DESC, id DESC).

    Order is load-bearing: ``owner_id`` first lets Postgres filter by
    owner, then walk the remaining columns already sorted for the
    list_my_recipes endpoint. ``created_at DESC`` yields newest-first
    rows without a separate sort, and ``id DESC`` is the stable
    tiebreaker for recipes that share a timestamp.
    """
    recipes_migration.run_migration(mock_connection)
    index_sql = next(
        s for s in (_normalize(x) for x in _executed_sql(mock_connection))
        if "create index" in s
    )
    pattern = (
        r"\(\s*owner_id\s*,\s*created_at\s+desc\s*,\s*id\s+desc\s*\)"
    )
    assert re.search(pattern, index_sql), (
        f"expected index columns `(owner_id, created_at DESC, id DESC)`; "
        f"got: {index_sql!r}"
    )


def test_compound_index_created_after_recipes_table(
    mock_connection: mock.MagicMock,
) -> None:
    """The index DDL fires AFTER the CREATE TABLE — it references ``recipes``.

    Creating an index against a not-yet-existing table fails with
    ``relation "recipes" does not exist``. Ordering is part of the
    migration's correctness contract.
    """
    recipes_migration.run_migration(mock_connection)
    stmts = [_normalize(s) for s in _executed_sql(mock_connection)]
    table_idx = next(i for i, s in enumerate(stmts) if "create table" in s)
    index_idx = next(i for i, s in enumerate(stmts) if "create index" in s)
    assert index_idx > table_idx, (
        f"CREATE INDEX (pos {index_idx}) must follow CREATE TABLE "
        f"(pos {table_idx}); execution order: {stmts!r}"
    )


def test_run_migration_does_not_swallow_exceptions(
    mock_connection: mock.MagicMock,
) -> None:
    """If the connection raises (e.g. missing ``users`` table), it propagates.

    The migration MUST NOT silently mask FK errors. A boot-time
    failure with a clear traceback beats a "successful" boot that
    leaves the schema broken.
    """
    boom = RuntimeError("users table missing")
    mock_connection.execute.side_effect = boom
    with pytest.raises(RuntimeError, match="users table missing"):
        recipes_migration.run_migration(mock_connection)


def test_module_docstring_documents_jsonb_choice() -> None:
    """The module docstring explains why ingredients/instructions are jsonb.

    Future-self / downstream agents need to know that the choice is
    intentional and motivated by structured-ingredient compatibility
    in later milestones — not an accidental data-type pick.
    """
    doc = (recipes_migration.__doc__ or "").lower()
    assert "jsonb" in doc, "module docstring should mention jsonb"
    assert "ingredient" in doc, "docstring should reference ingredients"


def test_module_docstring_documents_compound_index() -> None:
    """The module docstring explains the (owner_id, created_at DESC, id DESC) index.

    Recording the *why* of the compound index next to the migration
    keeps the rationale (list_my_recipes' sort path, stable name for
    idempotent re-runs) discoverable for the agents that come later.
    """
    doc = (recipes_migration.__doc__ or "").lower()
    assert "ix_recipes_owner_created_id" in doc, (
        "module docstring should name the compound index"
    )
    assert "owner_id" in doc, "docstring should mention owner_id"
    assert "created_at" in doc, "docstring should mention created_at"
