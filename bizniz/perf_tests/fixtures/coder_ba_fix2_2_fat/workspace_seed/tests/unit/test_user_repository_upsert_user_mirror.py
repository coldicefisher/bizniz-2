"""Unit tests for :func:`app.repositories.user_repository.upsert_user_mirror`.

The function emits a Postgres-only INSERT (``ON CONFLICT (id) DO
NOTHING ... RETURNING``) so the dialect cannot be exercised against
the sqlite engine the other unit tests use. Instead we drive the
function with a :class:`unittest.mock.MagicMock` standing in for the
SQLAlchemy ``Session`` and assert against the call surface:

* What the constructed statement looks like (lowercased email, the
  right columns, ON CONFLICT clause keyed on ``id``).
* That ``session.flush()`` is invoked after a successful execute.
* That the freshly-inserted row is returned when RETURNING produces
  a row.
* That the SELECT fallback fires when ON CONFLICT swallowed the
  insert (concurrent caller path).
* That an email-unique :class:`IntegrityError` is translated to
  :class:`DuplicateEmailInMirror` and the session is rolled back.
* That any other :class:`IntegrityError` (e.g. PK constraint) is
  re-raised unchanged so the original traceback survives.
* That non-IntegrityError DB failures bubble untouched.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects.postgresql.dml import OnConflictDoNothing
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.user import User
from app.repositories.user_repository import (
    DuplicateEmailInMirror,
    upsert_user_mirror,
)


def _make_integrity_error(constraint_name: str) -> IntegrityError:
    """Build an IntegrityError whose ``str(orig)`` contains ``constraint_name``.

    Drivers (asyncpg, psycopg) expose constraint names by embedding
    them in the error message. ``upsert_user_mirror`` substring-
    matches on ``users_email_key`` to distinguish the email-unique
    collision from a PK collision, so we just need ``str(e.orig)`` to
    carry the right substring.
    """
    orig = Exception(
        f'duplicate key value violates unique constraint "{constraint_name}"'
    )
    return IntegrityError("INSERT INTO users ...", {}, orig)


@pytest.mark.unit
def test_lowercases_email_before_insert() -> None:
    """Mixed-case email is lowercased BEFORE being bound to the INSERT.

    Per the contract, the case-insensitive unique constraint must
    never see the mixed-case input — production stores lowercase.
    We capture the executed statement and inspect the bound values.
    """
    fa_id = uuid.uuid4()
    captured_stmt = {}

    def _capture(stmt):
        captured_stmt["stmt"] = stmt
        r = MagicMock()
        r.scalar_one_or_none.return_value = MagicMock(spec=User)
        return r

    session = MagicMock()
    session.execute.side_effect = _capture

    upsert_user_mirror(session, fa_id, "Mixed@CASE.com", display_name="X")

    stmt = captured_stmt["stmt"]
    # Bound values for an Insert live on ``compile().params``.
    compiled = stmt.compile()
    assert compiled.params["email"] == "mixed@case.com"
    assert compiled.params["id"] == fa_id
    assert compiled.params["display_name"] == "X"


@pytest.mark.unit
def test_uses_on_conflict_do_nothing_on_id() -> None:
    """The statement is an ON CONFLICT DO NOTHING keyed on the ``id`` index.

    Inspect the constructed statement to verify the conflict clause
    is wired correctly — the wrong index would allow duplicate PKs
    through under load.
    """
    fa_id = uuid.uuid4()
    captured_stmt = {}

    def _capture(stmt):
        captured_stmt["stmt"] = stmt
        r = MagicMock()
        r.scalar_one_or_none.return_value = MagicMock(spec=User)
        return r

    session = MagicMock()
    session.execute.side_effect = _capture

    upsert_user_mirror(session, fa_id, "alice@example.com")

    stmt = captured_stmt["stmt"]
    on_conflict = stmt._post_values_clause
    assert isinstance(on_conflict, OnConflictDoNothing)
    # ``index_elements`` may be stored as plain strings (when passed
    # by name) or column objects (when passed as ``User.id``). Normalize
    # both shapes to names for the assertion.
    inferred_names = [
        c if isinstance(c, str) else c.name
        for c in on_conflict.inferred_target_elements
    ]
    assert inferred_names == ["id"]


@pytest.mark.unit
def test_returns_freshly_inserted_user_on_success() -> None:
    """When RETURNING yields a row, that row is returned and ``flush`` is called."""
    fa_id = uuid.uuid4()
    expected = MagicMock(spec=User)

    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = expected
    session.execute.return_value = result

    got = upsert_user_mirror(session, fa_id, "alice@example.com")

    assert got is expected
    session.flush.assert_called_once_with()
    # SELECT fallback must NOT fire on the happy path.
    assert session.execute.call_count == 1


@pytest.mark.unit
def test_default_role_is_user() -> None:
    """When ``role`` is omitted, the INSERT binds ``role='user'``."""
    fa_id = uuid.uuid4()
    captured_stmt = {}

    def _capture(stmt):
        captured_stmt["stmt"] = stmt
        r = MagicMock()
        r.scalar_one_or_none.return_value = MagicMock(spec=User)
        return r

    session = MagicMock()
    session.execute.side_effect = _capture

    upsert_user_mirror(session, fa_id, "alice@example.com")

    compiled = captured_stmt["stmt"].compile()
    assert compiled.params["role"] == "user"


@pytest.mark.unit
def test_select_fallback_on_concurrent_insert() -> None:
    """ON CONFLICT swallows the insert → SELECT fallback returns existing row.

    Two callers race; the second one's INSERT is no-op'd by ON CONFLICT
    (RETURNING yields zero rows), and the function falls through to a
    SELECT against the same id. The pre-existing row is returned.
    """
    fa_id = uuid.uuid4()
    existing = MagicMock(spec=User)

    insert_result = MagicMock()
    insert_result.scalar_one_or_none.return_value = None

    select_result = MagicMock()
    select_result.scalar_one.return_value = existing

    session = MagicMock()
    session.execute.side_effect = [insert_result, select_result]

    got = upsert_user_mirror(session, fa_id, "alice@example.com")

    assert got is existing
    assert session.execute.call_count == 2
    select_result.scalar_one.assert_called_once_with()


@pytest.mark.unit
def test_email_collision_raises_typed_exception_and_rolls_back() -> None:
    """Email-unique-index IntegrityError → DuplicateEmailInMirror + rollback.

    The original IntegrityError carries the constraint name in
    ``str(e.orig)``; ``upsert_user_mirror`` substring-matches on
    ``users_email_key`` and translates to the typed exception so
    the route layer can serve ``500 duplicate_email_in_mirror``.
    """
    fa_id = uuid.uuid4()
    session = MagicMock()
    session.execute.side_effect = _make_integrity_error("users_email_key")

    with pytest.raises(DuplicateEmailInMirror) as exc_info:
        upsert_user_mirror(session, fa_id, "Alice@Example.com")

    # email stored lowercased (matches what was attempted in the INSERT)
    assert exc_info.value.email == "alice@example.com"
    assert exc_info.value.attempted_id == fa_id
    session.rollback.assert_called_once_with()


@pytest.mark.unit
def test_pk_constraint_violation_reraises_integrity_error() -> None:
    """A non-email IntegrityError (e.g. PK) is re-raised unchanged.

    With ON CONFLICT (id) DO NOTHING this is unreachable in practice,
    but the defensive re-raise must preserve the original error so
    a future schema change that introduces a new unique constraint
    surfaces with its real diagnostic instead of being miscategorised
    as a duplicate-email error.
    """
    fa_id = uuid.uuid4()
    err = _make_integrity_error("users_pkey")

    session = MagicMock()
    session.execute.side_effect = err

    with pytest.raises(IntegrityError) as exc_info:
        upsert_user_mirror(session, fa_id, "alice@example.com")

    assert exc_info.value is err
    # MUST NOT translate to DuplicateEmailInMirror.
    assert not isinstance(exc_info.value, DuplicateEmailInMirror)


@pytest.mark.unit
def test_non_integrity_errors_bubble_unwrapped() -> None:
    """Non-IntegrityError SQLAlchemy errors propagate (no swallow, no translate).

    Connection drops, statement timeouts, etc. bubble so the route
    layer translates to ``503``. The repository must never catch
    broad ``Exception``.
    """
    fa_id = uuid.uuid4()
    session = MagicMock()
    session.execute.side_effect = OperationalError("stmt", {}, Exception("connection lost"))

    with pytest.raises(OperationalError):
        upsert_user_mirror(session, fa_id, "alice@example.com")

    # No rollback on the unhandled path — the caller owns transaction state.
    session.rollback.assert_not_called()


@pytest.mark.unit
def test_display_name_optional_binds_null() -> None:
    """Omitting ``display_name`` binds None to the INSERT."""
    fa_id = uuid.uuid4()
    captured_stmt = {}

    def _capture(stmt):
        captured_stmt["stmt"] = stmt
        r = MagicMock()
        r.scalar_one_or_none.return_value = MagicMock(spec=User)
        return r

    session = MagicMock()
    session.execute.side_effect = _capture

    upsert_user_mirror(session, fa_id, "alice@example.com")

    compiled = captured_stmt["stmt"].compile()
    assert compiled.params["display_name"] is None
