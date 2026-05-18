"""Unit tests for the ``get_current_user`` FastAPI dependency (BE-006-U5).

Exercises the full pipeline of the dependency without any live FA or
DB by monkeypatching the three upstream helpers
(``_validate_token_shape``, ``_decode_unverified_header``,
``_verify_jwt_signature_and_claims``) and the two repository
functions (``get_user_by_id``, ``upsert_user_mirror``) at the
``app.core.auth`` module seam — the same seam the previous BE-006
helper tests use, so behavior is verified in isolation from network
and Postgres.

Coverage:

* Happy path — existing local mirror row returns a populated
  :class:`CurrentUser` with role from JWT precedence (NOT from
  ``user.role``).
* Missing ``sub`` claim → 401 ``invalid_token``.
* Non-UUID ``sub`` claim → 401 ``invalid_token``.
* Missing ``roles`` claim → 403 ``no_role_assigned``.
* Empty ``roles`` list → 403 ``no_role_assigned``.
* ``roles`` with only unknown values → 403 ``no_role_assigned``
  (via ``_pick_role`` returning ``None``).
* Mirror auto-create on cold local DB → ``upsert_user_mirror`` called
  with the JWT email/display_name; ``db.commit`` awaited; INFO log
  ``mirror_autocreated`` emitted; returns CurrentUser.
* :class:`DuplicateEmailInMirror` from the auto-create → 500
  ``duplicate_email_in_mirror``.
* Role precedence (super_admin > admin > user) wins over local
  ``user.role`` — this is the load-bearing 'roles from JWT only'
  contract.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

# Settings() needs FUSIONAUTH_TENANT_ID even though this test never
# reads it (only used by the FA Settings class definition). The dev
# container env is missing it, so default it here for hermeticity.
# ``setdefault`` so a real env var still wins.
os.environ.setdefault(
    "FUSIONAUTH_TENANT_ID", "d4465dd9-12e7-4715-bc4e-690874974b6b"
)
os.environ.setdefault(
    "FUSIONAUTH_APPLICATION_ID", "85a03867-dccf-4882-adde-1a79aeec50df"
)
os.environ.setdefault("FUSIONAUTH_API_KEY", "test-api-key-xyz")

from fastapi import HTTPException

from app.core import auth as auth_module
from app.core.auth import CurrentUser, get_current_user
from app.repositories.user_repository import DuplicateEmailInMirror


SAMPLE_TOKEN = "header.payload.signature"
SAMPLE_HEADER = {"alg": "RS256", "kid": "kid-test"}
SAMPLE_SUB = "11111111-1111-1111-1111-111111111111"


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    claims: dict,
    *,
    token: str = SAMPLE_TOKEN,
    header: dict | None = None,
) -> None:
    """Patch the three pipeline helpers to deterministic outputs.

    The fourth step (DB lookup) is still patched per-test because
    each scenario wants different repo behaviour.
    """
    monkeypatch.setattr(
        auth_module, "_validate_token_shape", lambda authz: token
    )
    monkeypatch.setattr(
        auth_module,
        "_decode_unverified_header",
        lambda tok: header if header is not None else SAMPLE_HEADER,
    )

    async def _fake_verify(tok: str, hdr: dict) -> dict:
        return claims

    monkeypatch.setattr(
        auth_module, "_verify_jwt_signature_and_claims", _fake_verify
    )


def _patch_repo(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing_user: Any = None,
    upsert_result: Any = None,
    upsert_raises: BaseException | None = None,
) -> dict:
    """Patch the repository functions. Returns a capture dict.

    The capture dict records the kwargs passed to ``upsert_user_mirror``
    so tests can assert on the auto-create path.
    """
    captured: dict = {"upsert_called": False, "kwargs": {}}

    async def _fake_get_user_by_id(session, user_id):
        captured["get_called"] = True
        captured["get_user_id"] = user_id
        return existing_user

    def _fake_upsert(session, **kwargs):
        captured["upsert_called"] = True
        captured["kwargs"] = kwargs
        if upsert_raises is not None:
            raise upsert_raises
        return upsert_result

    monkeypatch.setattr(auth_module, "get_user_by_id", _fake_get_user_by_id)
    monkeypatch.setattr(auth_module, "upsert_user_mirror", _fake_upsert)
    return captured


def _fake_db() -> MagicMock:
    """A MagicMock standing in for the AsyncSession.

    Post-BA-fix1-1, the dependency drives the mirror upsert through
    ``await db.run_sync(lambda s: upsert_user_mirror(s, ...))``. We
    therefore expose ``run_sync`` as an async callable that invokes the
    lambda with a synthetic sync session — that's what the real
    AsyncSession does in production, just without the connection pool.
    ``commit`` and ``rollback`` are async no-ops so the dependency's
    ``await`` calls resolve cleanly.
    """
    db = MagicMock()
    db.sync_session = MagicMock()
    db.commit_called = False
    db.rollback_called = False

    async def _run_sync(fn, *args, **kwargs):
        return fn(db.sync_session, *args, **kwargs)

    async def _commit():
        db.commit_called = True

    async def _rollback():
        db.rollback_called = True

    db.run_sync = _run_sync
    db.commit = _commit
    db.rollback = _rollback
    return db


def _user_row(
    user_id: uuid.UUID,
    email: str = "alice@example.com",
    display_name: str | None = "Alice",
    role: str = "user",
) -> MagicMock:
    """A MagicMock User row exposing the attributes ``get_current_user`` reads."""
    user = MagicMock()
    user.id = user_id
    user.email = email
    user.display_name = display_name
    user.role = role
    return user


# ── Happy path ───────────────────────────────────────────


@pytest.mark.unit
class TestHappyPath:
    @pytest.mark.asyncio
    async def test_existing_user_returns_current_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub_uuid = uuid.UUID(SAMPLE_SUB)
        claims = {
            "sub": SAMPLE_SUB,
            "email": "alice@example.com",
            "roles": ["user"],
        }
        _patch_pipeline(monkeypatch, claims)
        _patch_repo(
            monkeypatch,
            existing_user=_user_row(sub_uuid, role="user"),
        )

        db = _fake_db()
        cu = await get_current_user(
            authorization=f"Bearer {SAMPLE_TOKEN}", db=db
        )

        assert isinstance(cu, CurrentUser)
        assert cu.id == sub_uuid
        assert cu.email == "alice@example.com"
        assert cu.display_name == "Alice"
        assert cu.role == "user"
        # No auto-create on the existing-user path.
        assert db.commit_called is False

    @pytest.mark.asyncio
    async def test_role_precedence_picks_super_admin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub_uuid = uuid.UUID(SAMPLE_SUB)
        claims = {
            "sub": SAMPLE_SUB,
            "email": "alice@example.com",
            "roles": ["user", "admin", "super_admin"],
        }
        _patch_pipeline(monkeypatch, claims)
        _patch_repo(monkeypatch, existing_user=_user_row(sub_uuid))

        cu = await get_current_user(
            authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
        )
        assert cu.role == "super_admin"

    @pytest.mark.asyncio
    async def test_role_from_jwt_not_from_db_user_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local user.role is 'admin' but JWT roles=['user'] → JWT wins.

        The load-bearing contract: authz reads the JWT precedence pick,
        NEVER the local mirror's ``role`` column (which is a snapshot
        and may lag).
        """
        sub_uuid = uuid.UUID(SAMPLE_SUB)
        claims = {
            "sub": SAMPLE_SUB,
            "email": "alice@example.com",
            # local says admin, JWT says only user → JWT wins.
            "roles": ["user"],
        }
        _patch_pipeline(monkeypatch, claims)
        _patch_repo(
            monkeypatch,
            existing_user=_user_row(sub_uuid, role="admin"),
        )

        cu = await get_current_user(
            authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
        )
        assert cu.role == "user"


# ── sub-claim validation ─────────────────────────────────


@pytest.mark.unit
class TestSubClaim:
    @pytest.mark.asyncio
    async def test_missing_sub_raises_invalid_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_pipeline(monkeypatch, {"roles": ["user"]})
        _patch_repo(monkeypatch)

        with pytest.raises(HTTPException) as exc:
            await get_current_user(
                authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
            )
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    @pytest.mark.asyncio
    async def test_empty_sub_raises_invalid_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_pipeline(monkeypatch, {"sub": "", "roles": ["user"]})
        _patch_repo(monkeypatch)

        with pytest.raises(HTTPException) as exc:
            await get_current_user(
                authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
            )
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    @pytest.mark.asyncio
    async def test_non_uuid_sub_raises_invalid_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_pipeline(
            monkeypatch, {"sub": "not-a-uuid", "roles": ["user"]}
        )
        _patch_repo(monkeypatch)

        with pytest.raises(HTTPException) as exc:
            await get_current_user(
                authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
            )
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}


# ── roles-claim validation ───────────────────────────────


@pytest.mark.unit
class TestRolesClaim:
    @pytest.mark.asyncio
    async def test_missing_roles_raises_no_role_assigned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_pipeline(monkeypatch, {"sub": SAMPLE_SUB})
        _patch_repo(monkeypatch)

        with pytest.raises(HTTPException) as exc:
            await get_current_user(
                authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
            )
        assert exc.value.status_code == 403
        assert exc.value.detail == {"error": "no_role_assigned"}

    @pytest.mark.asyncio
    async def test_empty_roles_raises_no_role_assigned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_pipeline(monkeypatch, {"sub": SAMPLE_SUB, "roles": []})
        _patch_repo(monkeypatch)

        with pytest.raises(HTTPException) as exc:
            await get_current_user(
                authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
            )
        assert exc.value.status_code == 403
        assert exc.value.detail == {"error": "no_role_assigned"}

    @pytest.mark.asyncio
    async def test_only_unknown_roles_raises_no_role_assigned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _pick_role returns None when none of super_admin / admin /
        # user appear, so this exercises the post-pick guard.
        _patch_pipeline(
            monkeypatch,
            {"sub": SAMPLE_SUB, "roles": ["viewer", "guest"]},
        )
        _patch_repo(monkeypatch)

        with pytest.raises(HTTPException) as exc:
            await get_current_user(
                authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
            )
        assert exc.value.status_code == 403
        assert exc.value.detail == {"error": "no_role_assigned"}


# ── Auto-create mirror path ──────────────────────────────


@pytest.mark.unit
class TestMirrorAutocreate:
    @pytest.mark.asyncio
    async def test_missing_local_row_triggers_upsert_and_commit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sub_uuid = uuid.UUID(SAMPLE_SUB)
        claims = {
            "sub": SAMPLE_SUB,
            "email": "Alice@Example.com",
            "name": "Alice Cook",
            "roles": ["user"],
        }
        created = _user_row(
            sub_uuid, email="alice@example.com", display_name="Alice Cook"
        )
        _patch_pipeline(monkeypatch, claims)
        captured = _patch_repo(
            monkeypatch,
            existing_user=None,
            upsert_result=created,
        )

        db = _fake_db()
        with caplog.at_level(logging.INFO, logger="app.core.auth"):
            cu = await get_current_user(
                authorization=f"Bearer {SAMPLE_TOKEN}", db=db
            )

        # upsert called with email + display_name from claims + role='user'
        assert captured["upsert_called"] is True
        kwargs = captured["kwargs"]
        assert kwargs["fa_user_id"] == sub_uuid
        assert kwargs["email"] == "Alice@Example.com"
        assert kwargs["role"] == "user"
        assert kwargs["display_name"] == "Alice Cook"
        # commit was awaited
        assert db.commit_called is True
        # INFO log emitted
        assert any(
            "mirror_autocreated" in r.message for r in caplog.records
        )
        # returned CurrentUser is populated from the freshly mirrored row
        assert cu.id == sub_uuid
        assert cu.email == "alice@example.com"
        assert cu.display_name == "Alice Cook"
        assert cu.role == "user"

    @pytest.mark.asyncio
    async def test_display_name_falls_back_to_preferred_username(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub_uuid = uuid.UUID(SAMPLE_SUB)
        claims = {
            "sub": SAMPLE_SUB,
            "email": "a@b.com",
            "preferred_username": "alice42",
            "roles": ["user"],
        }
        _patch_pipeline(monkeypatch, claims)
        captured = _patch_repo(
            monkeypatch,
            existing_user=None,
            upsert_result=_user_row(
                sub_uuid, email="a@b.com", display_name="alice42"
            ),
        )

        await get_current_user(
            authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
        )
        assert captured["kwargs"]["display_name"] == "alice42"

    @pytest.mark.asyncio
    async def test_missing_email_passes_empty_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub_uuid = uuid.UUID(SAMPLE_SUB)
        claims = {"sub": SAMPLE_SUB, "roles": ["user"]}
        _patch_pipeline(monkeypatch, claims)
        captured = _patch_repo(
            monkeypatch,
            existing_user=None,
            upsert_result=_user_row(
                sub_uuid, email="", display_name=None
            ),
        )

        await get_current_user(
            authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
        )
        assert captured["kwargs"]["email"] == ""

    @pytest.mark.asyncio
    async def test_duplicate_email_translates_to_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub_uuid = uuid.UUID(SAMPLE_SUB)
        claims = {
            "sub": SAMPLE_SUB,
            "email": "collide@example.com",
            "roles": ["user"],
        }
        _patch_pipeline(monkeypatch, claims)
        _patch_repo(
            monkeypatch,
            existing_user=None,
            upsert_raises=DuplicateEmailInMirror(
                email="collide@example.com", attempted_id=sub_uuid
            ),
        )

        with pytest.raises(HTTPException) as exc:
            await get_current_user(
                authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
            )

        assert exc.value.status_code == 500
        assert exc.value.detail == {"error": "duplicate_email_in_mirror"}

    @pytest.mark.asyncio
    async def test_db_exception_other_than_duplicate_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-typed DB error (e.g. OperationalError) bubbles unwrapped.

        The global exception handler translates this to 503
        ``database_unavailable`` — the dependency must NOT swallow it.
        """
        sub_uuid = uuid.UUID(SAMPLE_SUB)
        claims = {
            "sub": SAMPLE_SUB,
            "email": "x@y.com",
            "roles": ["user"],
        }
        _patch_pipeline(monkeypatch, claims)

        class _Boom(Exception):
            pass

        _patch_repo(
            monkeypatch,
            existing_user=None,
            upsert_raises=_Boom("connection lost"),
        )

        with pytest.raises(_Boom):
            await get_current_user(
                authorization=f"Bearer {SAMPLE_TOKEN}", db=_fake_db()
            )
