"""Repair-issue regression tests (BA-fix1-1).

Targets the bugs CodeReviewer flagged:

  * ``auth_signup`` / ``auth_login`` were calling the synchronous
    ``upsert_user_mirror`` directly against an AsyncSession yielded by
    ``get_db``. That returned an un-awaited coroutine from
    ``session.execute(stmt)`` and would AttributeError at runtime when
    the route tried to ``.scalar_one_or_none()`` on it. The fix is to
    bridge via ``await db.run_sync(lambda s: upsert_user_mirror(s, ...))``
    followed by ``await db.commit()``.
  * ``auth_signup`` returned 500 ``user_mirror_failed`` when the local
    mirror's email-unique constraint fired, masking what is really a
    user-fixable 409 ``email_already_registered``.
  * ``user_repository`` substring-matched the wrong constraint name
    (``users_email_key``) when the migration created the constraint as
    ``uq_users_email``, so the typed exception never fired against a
    real Postgres backend.
  * The ``_validate_token_shape`` helper rejected lowercase ``bearer``
    despite RFC 6750 §2.1 declaring the scheme name case-insensitive.
  * ``_sync_user_from_fusionauth`` + ``get_current_user_with_roles``
    referenced columns the new ``User`` model doesn't carry — dead-code
    landmine waiting to crash at import time the first time anything
    referenced them. Deleted entirely.
"""
# Settings instantiation requires several FA env vars at import time.
# Set safe defaults BEFORE importing app.core.config (which the route
# modules pull in transitively).
import os as _os

_os.environ.setdefault(
    "FUSIONAUTH_TENANT_ID", "d4465dd9-12e7-4715-bc4e-690874974b6b"
)
_os.environ.setdefault(
    "FUSIONAUTH_APPLICATION_ID", "85a03867-dccf-4882-adde-1a79aeec50df"
)
_os.environ.setdefault("FUSIONAUTH_API_KEY", "test-api-key-xyz")

import warnings
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.routes import auth_login as _login_mod
from app.api.routes import auth_signup as _signup_mod
from app.core import auth as _auth_mod
from app.core.auth import _validate_token_shape
from app.db.session import get_db
from app.repositories.user_repository import (
    DuplicateEmailInMirror,
    upsert_user_mirror,
)
from app.services.fusionauth_client import FusionAuthValidationError


_FA_USER_ID = "11111111-2222-3333-4444-555555555555"
_FA_TOKEN = "header.payload.signature"
_VALID_SIGNUP = {
    "email": "cook@example.com",
    "password": "Password123!",
    "display_name": "Cook",
}
_VALID_LOGIN = {"email": "user@example.com", "password": "password"}


def _make_user_row(
    user_id: str = _FA_USER_ID,
    email: str = "cook@example.com",
    display_name: str | None = "Cook",
    role: str = "user",
) -> MagicMock:
    """Build a stand-in for the User ORM row used by the route response."""
    row = MagicMock()
    row.id = UUID(user_id)
    row.email = email
    row.display_name = display_name
    row.role = role
    return row


class _FakeAsyncSession:
    """Minimal AsyncSession-like shim used by these route tests.

    The real ``get_db`` yields an :class:`AsyncSession`. The route under
    test bridges to synchronous ``upsert_user_mirror`` via
    ``await db.run_sync(lambda s: upsert_user_mirror(s, ...))``. To test
    the bridge correctly we need a fake whose ``run_sync`` is awaitable
    AND actually invokes the supplied callable (so the route's call to
    ``upsert_user_mirror`` is observable via the patched MagicMock).
    ``commit`` and ``rollback`` are awaitable no-ops backed by AsyncMock
    so the test can assert how often they were called.
    """

    def __init__(self) -> None:
        self.sync_session = MagicMock()
        self.run_sync = AsyncMock(side_effect=self._run_sync_impl)
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def _run_sync_impl(self, fn, *args, **kwargs):
        """Invoke ``fn`` with the synthetic sync session, return its value.

        Awaiting ``fn(self.sync_session)`` would be wrong — ``fn`` is the
        sync lambda the route hands us; calling it synchronously is the
        whole point of ``run_sync``.
        """
        return fn(self.sync_session, *args, **kwargs)


def _build_signup_client(session: _FakeAsyncSession) -> TestClient:
    """Spin up a TestClient mounting just the signup router."""
    app = FastAPI()
    app.include_router(_signup_mod.router, prefix="/api/v1")

    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _build_login_client(session: _FakeAsyncSession) -> TestClient:
    """Spin up a TestClient mounting just the login router."""
    app = FastAPI()
    app.include_router(_login_mod.router, prefix="/api/v1")

    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _install_login_jwt_validation(monkeypatch, *, claims: dict) -> None:
    """Patch the BE-006 JWT helpers used by the login route to succeed."""
    monkeypatch.setattr(
        _login_mod,
        "_decode_unverified_header",
        MagicMock(return_value={"alg": "RS256", "kid": "test-kid"}),
    )
    monkeypatch.setattr(
        _login_mod,
        "_verify_jwt_signature_and_claims",
        AsyncMock(return_value=claims),
    )


# ── signup: upsert routed via run_sync + commit awaited ────


class TestSignupAsyncBridge:
    """Signup MUST drive the mirror upsert through ``await db.run_sync``."""

    def test_signup_invokes_upsert_via_run_sync_and_returns_201(
        self, monkeypatch
    ):
        session = _FakeAsyncSession()
        user_row = _make_user_row()

        upsert_mock = MagicMock(return_value=user_row)
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {"id": _FA_USER_ID}}),
        )
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", upsert_mock)
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": "jwt.token.value"}),
        )

        # warnings.catch_warnings + simplefilter('error') turns the
        # "coroutine was never awaited" RuntimeWarning into an exception
        # so the assertion fails loudly if the sync-async fix regresses.
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            client = _build_signup_client(session)
            resp = client.post("/api/v1/auth/signup", json=_VALID_SIGNUP)

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["token"] == "jwt.token.value"
        assert body["user"]["id"] == _FA_USER_ID

        # The route MUST go through run_sync (the awaitable bridge),
        # not call upsert_user_mirror directly against the AsyncSession.
        session.run_sync.assert_awaited_once()
        # And the lambda passed to run_sync MUST be the one that invokes
        # upsert_user_mirror — we know it ran because the patched
        # MagicMock records the call.
        upsert_mock.assert_called_once()
        # ``commit`` is awaited, not called synchronously.
        session.commit.assert_awaited_once()

    def test_signup_upsert_called_with_sync_session_argument(
        self, monkeypatch
    ):
        # The lambda hands the SYNC session (the one ``run_sync`` injects)
        # to ``upsert_user_mirror`` — never the outer AsyncSession.
        session = _FakeAsyncSession()
        captured: list = []

        def _capture_upsert(s, **kwargs):
            captured.append((s, kwargs))
            return _make_user_row()

        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {"id": _FA_USER_ID}}),
        )
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", _capture_upsert)
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": "t"}),
        )

        client = _build_signup_client(session)
        resp = client.post("/api/v1/auth/signup", json=_VALID_SIGNUP)

        assert resp.status_code == 201, resp.text
        assert len(captured) == 1
        sync_arg, kwargs = captured[0]
        # The arg is the sync proxy session injected by run_sync — NOT
        # the AsyncSession. We can't isinstance-check easily across the
        # fake boundary, so just assert it's the sync_session attribute
        # we surfaced in _FakeAsyncSession.
        assert sync_arg is session.sync_session
        assert kwargs["fa_user_id"] == UUID(_FA_USER_ID)
        assert kwargs["email"] == "cook@example.com"
        assert kwargs["role"] == "user"
        assert kwargs["display_name"] == "Cook"


# ── login: same async bridge contract on the mirror auto-create path


class TestLoginAsyncBridge:
    """Login's mirror auto-create MUST drive the upsert via ``run_sync``."""

    def test_login_invokes_upsert_via_run_sync_and_returns_200(
        self, monkeypatch
    ):
        session = _FakeAsyncSession()

        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_login_jwt_validation(
            monkeypatch,
            claims={
                "sub": _FA_USER_ID,
                "email": "user@example.com",
                "roles": ["user"],
                "name": "Test User",
            },
        )
        # Force the mirror auto-create branch.
        monkeypatch.setattr(
            _login_mod, "get_user_by_id", AsyncMock(return_value=None)
        )
        upsert_mock = MagicMock(
            return_value=_make_user_row(
                email="user@example.com", display_name="Test User"
            )
        )
        monkeypatch.setattr(_login_mod, "upsert_user_mirror", upsert_mock)

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            client = _build_login_client(session)
            resp = client.post("/api/v1/auth/login", json=_VALID_LOGIN)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["token"] == _FA_TOKEN
        assert body["user"]["role"] == "user"

        session.run_sync.assert_awaited_once()
        upsert_mock.assert_called_once()
        session.commit.assert_awaited_once()


# ── signup: duplicate-mirror-email → 409 (NOT 500) ──────────


class TestSignupDuplicateMirrorEmailReturns409:
    """A ``DuplicateEmailInMirror`` from the upsert must surface as 409.

    Reviewer flagged the pre-repair behavior: 500 ``user_mirror_failed``
    masked what is really a user-resolvable 409
    ``email_already_registered``.
    """

    def test_duplicate_email_in_mirror_returns_409(self, monkeypatch):
        session = _FakeAsyncSession()

        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {"id": _FA_USER_ID}}),
        )

        def _raise_dup(s, **kwargs):
            raise DuplicateEmailInMirror(
                email="cook@example.com",
                attempted_id=UUID(_FA_USER_ID),
            )

        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", _raise_dup)
        login_mock = AsyncMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "login", login_mock
        )

        client = _build_signup_client(session)
        resp = client.post("/api/v1/auth/signup", json=_VALID_SIGNUP)

        assert resp.status_code == 409
        assert resp.json()["detail"] == {"error": "email_already_registered"}
        # FA login must NOT have been called once the mirror collision
        # was identified.
        login_mock.assert_not_called()


# ── repository: classify uq_users_email IntegrityError correctly ─


class TestUpsertUserMirrorClassifiesUqUsersEmail:
    """A real-shaped Postgres error for the email-unique violation must
    surface as :class:`DuplicateEmailInMirror`, not raw IntegrityError.

    The migration in ``alembic/versions/0001_create_users.py`` names the
    constraint ``uq_users_email``. Pre-repair the repository scanned for
    ``users_email_key`` only — so against the real schema the typed
    exception never fired and the route layer got an unwrapped
    IntegrityError instead.
    """

    def _make_orig_error(self, message: str) -> Exception:
        class _Orig(Exception):
            def __str__(self) -> str:
                return message

        return _Orig(message)

    def test_uq_users_email_orig_text_classified(self):
        orig = self._make_orig_error(
            'duplicate key value violates unique constraint "uq_users_email"\n'
            "DETAIL:  Key (email)=(dup@example.com) already exists."
        )
        boom = IntegrityError("stmt", {}, orig)
        session = MagicMock()
        session.execute.side_effect = boom

        fa_id = uuid4()
        with pytest.raises(DuplicateEmailInMirror) as exc_info:
            upsert_user_mirror(session, fa_id, "dup@example.com")

        assert exc_info.value.email == "dup@example.com"
        assert exc_info.value.attempted_id == fa_id
        # Rollback on the classified path keeps the caller transaction
        # usable.
        session.rollback.assert_called_once()

    def test_legacy_users_email_key_orig_text_still_classified(self):
        # Defense: older driver / naming-convention paths surfaced the
        # constraint as ``users_email_key``. The repair MUST keep that
        # path working too.
        orig = self._make_orig_error(
            'duplicate key value violates unique constraint "users_email_key"'
        )
        boom = IntegrityError("stmt", {}, orig)
        session = MagicMock()
        session.execute.side_effect = boom

        with pytest.raises(DuplicateEmailInMirror):
            upsert_user_mirror(session, uuid4(), "dup@example.com")

    def test_unrelated_integrity_error_bubbles_unwrapped(self):
        # A non-email IntegrityError (e.g. role check constraint) must
        # NOT be misclassified as a duplicate email.
        orig = self._make_orig_error(
            'new row for relation "users" violates check constraint "ck_users_role"'
        )
        boom = IntegrityError("stmt", {}, orig)
        session = MagicMock()
        session.execute.side_effect = boom

        with pytest.raises(IntegrityError) as exc_info:
            upsert_user_mirror(session, uuid4(), "ok@example.com")
        assert exc_info.value is boom


# ── auth: Bearer prefix is case-insensitive ─────────────────


class TestValidateTokenShapeCaseInsensitiveBearer:
    """RFC 6750 §2.1: scheme name match is case-insensitive."""

    def test_lowercase_bearer_accepted(self):
        result = _validate_token_shape("bearer abc.def.ghi")
        assert result == "abc.def.ghi"

    def test_uppercase_bearer_accepted(self):
        result = _validate_token_shape("BEARER abc.def.ghi")
        assert result == "abc.def.ghi"

    def test_mixed_case_bearer_accepted(self):
        result = _validate_token_shape("BeArEr abc.def.ghi")
        assert result == "abc.def.ghi"

    def test_titlecase_bearer_still_accepted(self):
        result = _validate_token_shape("Bearer abc.def.ghi")
        assert result == "abc.def.ghi"

    def test_basic_scheme_still_rejected(self):
        # The case-insensitive match applies to the literal "bearer"
        # token — other schemes still fail with 401 unauthenticated.
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape("Basic abc.def.ghi")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "unauthenticated"}


# ── auth: dead code removed ─────────────────────────────────


class TestDeadCodeRemoved:
    """``_sync_user_from_fusionauth`` and ``get_current_user_with_roles``
    referenced columns the new ``User`` model doesn't carry — they were
    dead-code landmines waiting to crash at import time the moment any
    caller tried to use them. The repair deletes them outright; these
    sentinels guard against an accidental re-addition.
    """

    def test_sync_user_from_fusionauth_is_gone(self):
        assert "_sync_user_from_fusionauth" not in _auth_mod.__dict__
        assert not hasattr(_auth_mod, "_sync_user_from_fusionauth")

    def test_get_current_user_with_roles_is_gone(self):
        assert "get_current_user_with_roles" not in _auth_mod.__dict__
        assert not hasattr(_auth_mod, "get_current_user_with_roles")
