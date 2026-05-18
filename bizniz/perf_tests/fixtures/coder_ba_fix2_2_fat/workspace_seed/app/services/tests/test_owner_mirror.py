"""Unit tests for :func:`app.services.owner_mirror.ensure_local_user`.

The helper is the self-heal entry point used by downstream route
handlers (create_recipe, etc.) to guarantee a local ``users`` mirror
row exists for the JWT subject before they FK to it.

Two paths to cover:

* **Fast path** — row already exists: helper returns the parsed UUID
  without invoking ``upsert_user_mirror``.
* **Self-heal path** — row missing: helper bridges to the sync
  ``upsert_user_mirror`` via ``session.run_sync(...)`` (matching the
  milestone-1 ``auth_login`` / ``/api/me`` pattern) and returns the
  parsed UUID.

Contract assertions worth pinning:

* The helper NEVER calls ``session.commit()`` — the surrounding
  transaction owns commit (matches ``upsert_user_mirror``'s contract).
* ``role='user'`` is forced — the JWT roles claim is authoritative for
  authz; the mirror role column is informational only.
* ``display_name`` falls back from ``name`` → ``preferred_username``
  → ``None``.
* ``DuplicateEmailInMirror`` (and any other DB exception) propagates
  unwrapped so the route layer can translate to its preferred
  status code.

All tests are mock-driven — no DB required. The interaction we care
about is the shape of calls made against ``get_user_by_id`` /
``upsert_user_mirror`` / ``session.run_sync``, not the actual SQL.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.repositories.user_repository import DuplicateEmailInMirror
from app.services import owner_mirror
from app.services.owner_mirror import ensure_local_user


_FA_USER_ID = "11111111-2222-3333-4444-555555555555"
_FA_UUID = UUID(_FA_USER_ID)
_EMAIL = "user@example.com"


def _async_session_with_run_sync() -> MagicMock:
    """Return a MagicMock session whose ``run_sync`` awaits a callable.

    The helper uses ``await session.run_sync(lambda s: upsert_user_mirror(s, ...))``
    to bridge the sync repo function onto the AsyncSession. Plain
    MagicMocks return non-awaitable MagicMocks from ``run_sync``, so we
    replace it with an AsyncMock whose side_effect invokes the passed
    callable against the session itself (mirroring what the real
    ``AsyncSession.run_sync`` does).
    """
    session = MagicMock(name="AsyncSession")

    async def _run_sync(fn, *args, **kwargs):
        return fn(session, *args, **kwargs)

    session.run_sync = AsyncMock(side_effect=_run_sync)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_owner_id_when_user_exists(monkeypatch) -> None:
    """Fast path: existing row → return UUID, never call upsert."""
    session = _async_session_with_run_sync()

    existing_user = MagicMock(name="User", id=_FA_UUID, email=_EMAIL)
    get_by_id = AsyncMock(return_value=existing_user)
    upsert = MagicMock(name="upsert_user_mirror")

    monkeypatch.setattr(owner_mirror, "get_user_by_id", get_by_id)
    monkeypatch.setattr(owner_mirror, "upsert_user_mirror", upsert)

    result = await ensure_local_user(
        session, jwt_claims={"sub": _FA_USER_ID, "email": _EMAIL}
    )

    assert result == _FA_UUID
    get_by_id.assert_awaited_once_with(session, _FA_UUID)
    # No self-heal needed.
    upsert.assert_not_called()
    session.run_sync.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_self_heals_when_user_missing(monkeypatch) -> None:
    """Self-heal path: missing row → upsert is invoked, UUID returned."""
    session = _async_session_with_run_sync()

    get_by_id = AsyncMock(return_value=None)
    upsert = MagicMock(name="upsert_user_mirror", return_value=MagicMock())

    monkeypatch.setattr(owner_mirror, "get_user_by_id", get_by_id)
    monkeypatch.setattr(owner_mirror, "upsert_user_mirror", upsert)

    result = await ensure_local_user(
        session,
        jwt_claims={
            "sub": _FA_USER_ID,
            "email": _EMAIL,
            "name": "Test User",
        },
    )

    assert result == _FA_UUID
    get_by_id.assert_awaited_once_with(session, _FA_UUID)
    session.run_sync.assert_awaited_once()
    upsert.assert_called_once()
    kwargs = upsert.call_args.kwargs
    assert kwargs["fa_user_id"] == _FA_UUID
    assert kwargs["email"] == _EMAIL
    assert kwargs["role"] == "user"
    assert kwargs["display_name"] == "Test User"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_display_name_falls_back_to_preferred_username(
    monkeypatch,
) -> None:
    """No ``name`` claim → fall back to ``preferred_username``."""
    session = _async_session_with_run_sync()

    monkeypatch.setattr(
        owner_mirror, "get_user_by_id", AsyncMock(return_value=None)
    )
    upsert = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(owner_mirror, "upsert_user_mirror", upsert)

    await ensure_local_user(
        session,
        jwt_claims={
            "sub": _FA_USER_ID,
            "email": _EMAIL,
            "preferred_username": "fallback",
        },
    )

    assert upsert.call_args.kwargs["display_name"] == "fallback"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_display_name_none_when_neither_claim_present(
    monkeypatch,
) -> None:
    """Neither ``name`` nor ``preferred_username`` → display_name=None."""
    session = _async_session_with_run_sync()

    monkeypatch.setattr(
        owner_mirror, "get_user_by_id", AsyncMock(return_value=None)
    )
    upsert = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(owner_mirror, "upsert_user_mirror", upsert)

    await ensure_local_user(
        session, jwt_claims={"sub": _FA_USER_ID, "email": _EMAIL}
    )

    assert upsert.call_args.kwargs["display_name"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_role_forced_to_user_even_if_claim_says_admin(
    monkeypatch,
) -> None:
    """JWT roles do NOT leak into the mirror — column is always 'user'.

    The JWT roles claim remains authoritative for authz; the mirror
    role column is informational only. Matches the milestone-1
    auth_login / /api/me contract.
    """
    session = _async_session_with_run_sync()

    monkeypatch.setattr(
        owner_mirror, "get_user_by_id", AsyncMock(return_value=None)
    )
    upsert = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(owner_mirror, "upsert_user_mirror", upsert)

    await ensure_local_user(
        session,
        jwt_claims={
            "sub": _FA_USER_ID,
            "email": _EMAIL,
            "roles": ["admin", "super_admin"],
        },
    )

    assert upsert.call_args.kwargs["role"] == "user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_does_not_commit_in_either_path(monkeypatch) -> None:
    """Helper NEVER calls ``session.commit()``. The surrounding
    transaction owns commit/rollback — matches ``upsert_user_mirror``.
    """
    # Fast path
    session1 = _async_session_with_run_sync()
    monkeypatch.setattr(
        owner_mirror,
        "get_user_by_id",
        AsyncMock(return_value=MagicMock(id=_FA_UUID)),
    )
    await ensure_local_user(
        session1, jwt_claims={"sub": _FA_USER_ID, "email": _EMAIL}
    )
    session1.commit.assert_not_called()
    session1.rollback.assert_not_called()

    # Self-heal path
    session2 = _async_session_with_run_sync()
    monkeypatch.setattr(
        owner_mirror, "get_user_by_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        owner_mirror,
        "upsert_user_mirror",
        MagicMock(return_value=MagicMock()),
    )
    await ensure_local_user(
        session2, jwt_claims={"sub": _FA_USER_ID, "email": _EMAIL}
    )
    session2.commit.assert_not_called()
    session2.rollback.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_email_in_mirror_propagates(monkeypatch) -> None:
    """``DuplicateEmailInMirror`` from upsert bubbles unwrapped.

    The helper does NOT translate to HTTPException — the route layer
    owns the status code. Locks in the contract that swallowing or
    re-wrapping here would steal that decision from the caller.
    """
    session = _async_session_with_run_sync()
    monkeypatch.setattr(
        owner_mirror, "get_user_by_id", AsyncMock(return_value=None)
    )
    exc = DuplicateEmailInMirror(email=_EMAIL, attempted_id=_FA_UUID)
    monkeypatch.setattr(
        owner_mirror, "upsert_user_mirror", MagicMock(side_effect=exc)
    )

    with pytest.raises(DuplicateEmailInMirror) as exc_info:
        await ensure_local_user(
            session, jwt_claims={"sub": _FA_USER_ID, "email": _EMAIL}
        )

    assert exc_info.value is exc


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_sub_raises_value_error(monkeypatch) -> None:
    """``sub`` that doesn't parse as UUID → ValueError (caller must
    have validated JWT shape before calling this helper).
    """
    session = _async_session_with_run_sync()
    monkeypatch.setattr(
        owner_mirror, "get_user_by_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        owner_mirror,
        "upsert_user_mirror",
        MagicMock(return_value=MagicMock()),
    )

    with pytest.raises(ValueError):
        await ensure_local_user(
            session,
            jwt_claims={"sub": "not-a-uuid", "email": _EMAIL},
        )
