"""Unit tests for the 0001_create_users Alembic migration.

These tests verify the migration's *intent* by mocking the
``alembic.op`` interface and inspecting which operations are
issued. They do not connect to Postgres — the live-DB behavior is
covered by the lifespan ``create_all`` path and integration tests
in later milestones.

What's enforced here:

- Revision metadata: revision id, down_revision (baseline), so
  Alembic's linear-migration model resolves correctly.
- Upgrade order: ``CREATE EXTENSION citext`` MUST run before
  ``create_table('users')`` because the ``email`` column's CITEXT
  type fails to resolve otherwise.
- Column shape: every column from the U1 ORM model (id, email,
  role, display_name, created_at, updated_at) is present with the
  expected type / nullability / default.
- Constraint shape: PK on id, UNIQUE on email, CHECK on role
  restricting it to {'user', 'admin', 'super_admin'}.
- Downgrade order: ``drop_table('users')`` MUST run before
  ``DROP EXTENSION citext`` — dropping the extension while the
  table still references CITEXT would error.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0001_create_users.py"
)


def _load_migration_module() -> Any:
    """Import the migration as a standalone module.

    Alembic versions are not a regular Python package (no
    ``__init__.py``), so we load by file path instead of
    ``import alembic.versions.0001_create_users``.
    """
    spec = importlib.util.spec_from_file_location(
        "migration_0001_create_users", str(MIGRATION_PATH)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_file_exists() -> None:
    """The migration file ships at the expected path."""
    assert MIGRATION_PATH.is_file(), (
        f"expected migration at {MIGRATION_PATH}, but not found"
    )


def test_revision_metadata() -> None:
    """Revision id and down_revision form a valid baseline migration."""
    mod = _load_migration_module()
    assert mod.revision == "0001_create_users"
    assert mod.down_revision is None


def test_module_exposes_upgrade_and_downgrade() -> None:
    """Both upgrade() and downgrade() are callable on the module."""
    mod = _load_migration_module()
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def _run_upgrade_with_mock_op() -> mock.MagicMock:
    """Execute ``upgrade()`` against a mocked ``alembic.op``.

    Returns the mock so the caller can introspect the calls that
    were issued.
    """
    mod = _load_migration_module()
    with mock.patch.object(mod, "op") as mock_op:
        mod.upgrade()
    return mock_op


def _run_downgrade_with_mock_op() -> mock.MagicMock:
    """Execute ``downgrade()`` against a mocked ``alembic.op``."""
    mod = _load_migration_module()
    with mock.patch.object(mod, "op") as mock_op:
        mod.downgrade()
    return mock_op


def test_upgrade_creates_citext_extension_first() -> None:
    """``CREATE EXTENSION IF NOT EXISTS citext`` precedes create_table.

    Order matters: the ``email`` column uses the CITEXT type, which
    only resolves after the extension exists.
    """
    mock_op = _run_upgrade_with_mock_op()

    method_call_order = [c[0] for c in mock_op.mock_calls if "." not in c[0]]
    # First top-level call should be op.execute(...) for the extension.
    assert method_call_order, "expected at least one op.* call"
    assert method_call_order[0] == "execute", (
        f"expected first op call to be 'execute', got {method_call_order[0]!r}"
    )

    first_execute_sql = mock_op.execute.call_args_list[0].args[0]
    assert "CREATE EXTENSION" in first_execute_sql.upper()
    assert "CITEXT" in first_execute_sql.upper()
    assert "IF NOT EXISTS" in first_execute_sql.upper()

    # create_table runs after the extension exists.
    assert mock_op.create_table.called, "upgrade() must call create_table"
    create_idx = method_call_order.index("create_table")
    execute_idx = method_call_order.index("execute")
    assert execute_idx < create_idx, (
        "CREATE EXTENSION must run before create_table('users')"
    )


def test_upgrade_creates_users_table() -> None:
    """``create_table`` is called with table name 'users'."""
    mock_op = _run_upgrade_with_mock_op()
    assert mock_op.create_table.call_count == 1
    args, _kwargs = mock_op.create_table.call_args
    assert args[0] == "users"


def _columns_by_name(mock_op: mock.MagicMock) -> dict[str, sa.Column]:
    """Extract sa.Column objects from the create_table call by name."""
    args, _kwargs = mock_op.create_table.call_args
    return {a.name: a for a in args[1:] if isinstance(a, sa.Column)}


def test_upgrade_users_table_has_all_columns() -> None:
    """All six U1 columns are declared on the table."""
    mock_op = _run_upgrade_with_mock_op()
    cols = _columns_by_name(mock_op)
    assert set(cols.keys()) == {
        "id",
        "email",
        "role",
        "display_name",
        "created_at",
        "updated_at",
    }


def test_users_id_column_is_uuid_primary_key() -> None:
    """``id`` is a Postgres UUID column flagged as primary key."""
    mock_op = _run_upgrade_with_mock_op()
    cols = _columns_by_name(mock_op)
    id_col = cols["id"]
    assert isinstance(id_col.type, PG_UUID)
    assert id_col.type.as_uuid is True
    assert id_col.primary_key is True
    assert id_col.nullable is False


def test_users_email_column_is_citext_254_not_null() -> None:
    """``email`` is CITEXT(254), NOT NULL."""
    mock_op = _run_upgrade_with_mock_op()
    cols = _columns_by_name(mock_op)
    email_col = cols["email"]
    assert isinstance(email_col.type, CITEXT)
    assert email_col.type.length == 254
    assert email_col.nullable is False


def test_users_role_column_has_default_and_length() -> None:
    """``role`` is VARCHAR(20), NOT NULL, server_default='user'."""
    mock_op = _run_upgrade_with_mock_op()
    cols = _columns_by_name(mock_op)
    role_col = cols["role"]
    assert isinstance(role_col.type, sa.String)
    assert role_col.type.length == 20
    assert role_col.nullable is False
    assert role_col.server_default is not None
    # server_default.arg can be a string or a TextClause.
    default_value = role_col.server_default.arg
    default_str = (
        default_value if isinstance(default_value, str) else str(default_value)
    )
    assert "user" in default_str


def test_users_display_name_column_is_nullable_varchar_100() -> None:
    """``display_name`` is VARCHAR(100), NULL."""
    mock_op = _run_upgrade_with_mock_op()
    cols = _columns_by_name(mock_op)
    dn_col = cols["display_name"]
    assert isinstance(dn_col.type, sa.String)
    assert dn_col.type.length == 100
    assert dn_col.nullable is True


@pytest.mark.parametrize("col_name", ["created_at", "updated_at"])
def test_users_timestamp_columns_are_tz_not_null_with_default(
    col_name: str,
) -> None:
    """``created_at`` and ``updated_at`` are TIMESTAMPTZ NOT NULL DEFAULT now()."""
    mock_op = _run_upgrade_with_mock_op()
    cols = _columns_by_name(mock_op)
    ts_col = cols[col_name]
    assert isinstance(ts_col.type, sa.DateTime)
    assert ts_col.type.timezone is True
    assert ts_col.nullable is False
    assert ts_col.server_default is not None
    default_value = ts_col.server_default.arg
    default_str = (
        default_value if isinstance(default_value, str) else str(default_value)
    )
    assert "now()" in default_str.lower()


def _table_constraints(mock_op: mock.MagicMock) -> list[Any]:
    """Return non-column positional args from the create_table call."""
    args, _kwargs = mock_op.create_table.call_args
    return [a for a in args[1:] if not isinstance(a, sa.Column)]


def test_users_table_has_role_check_constraint() -> None:
    """A CheckConstraint restricts ``role`` to the three permitted values."""
    mock_op = _run_upgrade_with_mock_op()
    constraints = _table_constraints(mock_op)
    check_constraints = [
        c for c in constraints if isinstance(c, sa.CheckConstraint)
    ]
    assert check_constraints, "expected a CheckConstraint on role"
    sqltexts = [str(c.sqltext) for c in check_constraints]
    assert any(
        "user" in s and "admin" in s and "super_admin" in s for s in sqltexts
    ), f"role CHECK constraint missing expected values; got {sqltexts!r}"


def test_users_table_has_unique_email_constraint() -> None:
    """A UniqueConstraint is declared on the ``email`` column.

    Either the column-level ``unique=True`` or an explicit
    ``UniqueConstraint('email')`` satisfies this — the migration
    uses the explicit form so the constraint name is stable across
    autogenerate runs.
    """
    mock_op = _run_upgrade_with_mock_op()
    cols = _columns_by_name(mock_op)
    constraints = _table_constraints(mock_op)
    unique_constraints = [
        c for c in constraints if isinstance(c, sa.UniqueConstraint)
    ]

    def _uq_column_names(uq: sa.UniqueConstraint) -> list[str]:
        # An unbound UniqueConstraint stores positional column args in
        # ``_pending_colargs`` until the table binds it; ``columns`` is
        # empty in that state. Fall back to both.
        names = [col.name for col in uq.columns]
        if names:
            return names
        pending = getattr(uq, "_pending_colargs", []) or []
        return [c if isinstance(c, str) else getattr(c, "name", "") for c in pending]

    has_explicit_unique = any(
        "email" in _uq_column_names(c) for c in unique_constraints
    )
    has_column_unique = cols["email"].unique is True
    assert has_explicit_unique or has_column_unique, (
        "expected a UniqueConstraint on email (column- or table-level)"
    )


def test_downgrade_drops_table_then_extension() -> None:
    """``drop_table('users')`` runs before ``DROP EXTENSION citext``.

    Reversing this order would fail: Postgres refuses to drop an
    extension while a column type still references it.
    """
    mock_op = _run_downgrade_with_mock_op()

    method_call_order = [c[0] for c in mock_op.mock_calls if "." not in c[0]]
    assert "drop_table" in method_call_order
    assert "execute" in method_call_order

    drop_idx = method_call_order.index("drop_table")
    execute_idx = method_call_order.index("execute")
    assert drop_idx < execute_idx, (
        "drop_table('users') must run before DROP EXTENSION citext"
    )

    args, _kwargs = mock_op.drop_table.call_args
    assert args[0] == "users"

    drop_sql = mock_op.execute.call_args.args[0]
    assert "DROP EXTENSION" in drop_sql.upper()
    assert "CITEXT" in drop_sql.upper()
    assert "IF EXISTS" in drop_sql.upper()
