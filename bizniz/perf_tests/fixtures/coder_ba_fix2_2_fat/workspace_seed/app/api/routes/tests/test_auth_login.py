"""Unit tests for the auth_login module scaffold (BE-008-U1).

Covers the module-level invariants set by the scaffold issue:

* ``router`` is a properly-configured ``APIRouter`` whose prefix and
  tags match BE-007's auto-mount contract (auto-mount under
  ``settings.api_v1_prefix`` adds ``/api/v1`` — the router declares
  only ``/auth`` to avoid double-prefixing, matching
  ``app/api/routes/auth_signup.py``).
* The JWT-validation helpers from BE-006 (``app.core.auth``) are
  imported by reference here — NOT re-implemented — so the
  algorithm-pinning + JWKS-cache invariants are preserved when
  BE-008-U2 plugs in the route handler.
* ``_build_user_out_from_claims`` constructs a ``UserOut`` with role
  taken from the JWT (NOT from the mirror row), per BE-006's contract
  that the JWT is authoritative for authorization.

The full route handler tests land in BE-008-U2 and beyond.
"""
# Settings() requires FUSIONAUTH_APPLICATION_ID, _TENANT_ID, _API_KEY
# even though they aren't referenced by this scaffold. The dev
# container env is missing them in some test runners, so fill in safe
# defaults BEFORE importing app.core.config (which instantiates the
# module-level singleton at import time). setdefault means a real env
# var still wins if one is present.
import os as _os

_os.environ.setdefault(
    "FUSIONAUTH_TENANT_ID", "d4465dd9-12e7-4715-bc4e-690874974b6b"
)
_os.environ.setdefault(
    "FUSIONAUTH_APPLICATION_ID", "85a03867-dccf-4882-adde-1a79aeec50df"
)
_os.environ.setdefault("FUSIONAUTH_API_KEY", "test-api-key-xyz")

import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import APIRouter

from app.api.routes import auth_login
from app.api.routes.auth_login import (
    _build_user_out_from_claims,
    router,
)


# ── Router scaffold ───────────────────────────────────────


class TestRouterScaffold:
    """The router declaration is the skeleton's only public contract."""

    def test_router_is_apirouter(self):
        assert isinstance(router, APIRouter)

    def test_router_prefix_is_auth_only(self):
        # Skeleton auto-mount adds /api/v1 — declaring /api/auth here
        # would double-prefix to /api/v1/api/auth. Match the existing
        # app/api/routes/auth_signup.py convention: prefix='/auth'.
        assert router.prefix == "/auth"

    def test_router_tags_include_auth(self):
        assert router.tags == ["auth"]

    def test_router_prefix_matches_signup_router(self):
        # BE-008's spec is explicit that login + signup MUST agree on
        # the prefix shape — otherwise one route ends up at
        # /api/v1/auth/X and the other at /api/v1/api/auth/X and the
        # SPA hits 404s on whichever one drifted.
        from app.api.routes.auth_signup import router as signup_router
        assert router.prefix == signup_router.prefix
        assert router.tags == signup_router.tags


# ── Imports the route handler will need ──────────────────


class TestModuleImports:
    """U2 needs these symbols importable at module load time."""

    def test_login_request_imported(self):
        from app.schemas.auth import LoginRequest as _expected
        assert auth_login.LoginRequest is _expected

    def test_auth_response_imported(self):
        from app.schemas.auth import AuthResponse as _expected
        assert auth_login.AuthResponse is _expected

    def test_user_out_imported(self):
        from app.schemas.auth import UserOut as _expected
        assert auth_login.UserOut is _expected

    def test_error_response_imported(self):
        from app.schemas.auth import ErrorResponse as _expected
        assert auth_login.ErrorResponse is _expected

    def test_fusionauth_client_module_imported(self):
        from app.services import fusionauth_client as _fa
        assert auth_login.fusionauth_client is _fa

    def test_fa_exceptions_imported(self):
        from app.services.fusionauth_client import (
            FusionAuthUnavailable as _u,
            FusionAuthValidationError as _v,
        )
        assert auth_login.FusionAuthUnavailable is _u
        assert auth_login.FusionAuthValidationError is _v

    def test_repository_get_user_by_id_imported(self):
        from app.repositories.user_repository import (
            get_user_by_id as _get_user_by_id,
        )
        assert auth_login.get_user_by_id is _get_user_by_id

    def test_repository_upsert_user_mirror_imported(self):
        from app.repositories.user_repository import (
            upsert_user_mirror as _upsert,
        )
        assert auth_login.upsert_user_mirror is _upsert

    def test_repository_duplicate_email_imported(self):
        from app.repositories.user_repository import (
            DuplicateEmailInMirror as _dup,
        )
        assert auth_login.DuplicateEmailInMirror is _dup

    def test_get_db_imported(self):
        from app.db.session import get_db as _get_db
        assert auth_login.get_db is _get_db

    def test_logger_named_for_module(self):
        assert isinstance(auth_login.logger, logging.Logger)
        assert auth_login.logger.name == "app.api.routes.auth_login"


# ── JWT-validation helper extraction check ───────────────


class TestJwtValidationHelperReuse:
    """Step 5 of BE-008: reuse BE-006's JWT-validation helpers.

    Re-implementing JWT parsing here would silently lose BE-006's
    algorithm-pinning + JWKS-cache invariants. These tests assert that
    auth_login imports the canonical helpers from app.core.auth (the
    middleware's own validator) BY REFERENCE — same function object,
    not a copy.
    """

    def test_decode_unverified_header_helper_imported(self):
        from app.core.auth import (
            _decode_unverified_header as _expected,
        )
        assert auth_login._decode_unverified_header is _expected

    def test_verify_jwt_signature_and_claims_helper_imported(self):
        from app.core.auth import (
            _verify_jwt_signature_and_claims as _expected,
        )
        assert auth_login._verify_jwt_signature_and_claims is _expected


# ── _build_user_out_from_claims ──────────────────────────


class TestBuildUserOutFromClaims:
    """Role MUST come from the JWT, not from the mirror row.

    BE-006's contract is that the JWT is authoritative for
    authorization; the mirror's ``role`` column is a snapshot for
    diagnostics only. Using the mirror row's role here would hand the
    SPA a stale role if FusionAuth changed the user's roles after the
    mirror was written.
    """

    def _make_row(self, **overrides):
        defaults = dict(
            id=uuid.UUID("85a03867-dccf-4882-adde-1a79aeec50df"),
            email="user@example.com",
            display_name="Test User",
            role="user",
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_returns_user_out_instance(self):
        from app.schemas.auth import UserOut
        row = self._make_row()
        result = _build_user_out_from_claims(row, jwt_role="user")
        assert isinstance(result, UserOut)

    def test_copies_id_email_display_name_from_row(self):
        row = self._make_row(
            id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
            email="cook@example.com",
            display_name="Cook",
        )
        result = _build_user_out_from_claims(row, jwt_role="user")
        assert result.id == uuid.UUID("11111111-2222-3333-4444-555555555555")
        assert result.email == "cook@example.com"
        assert result.display_name == "Cook"

    def test_role_taken_from_jwt_not_row(self):
        # The mirror row says 'user' but the JWT says 'admin' — the
        # JWT wins, because BE-006 makes the JWT authoritative for
        # authorization. The mirror's role column is just diagnostic.
        row = self._make_row(role="user")
        result = _build_user_out_from_claims(row, jwt_role="admin")
        assert result.role == "admin"

    def test_role_super_admin_from_jwt(self):
        row = self._make_row(role="user")
        result = _build_user_out_from_claims(row, jwt_role="super_admin")
        assert result.role == "super_admin"

    def test_display_name_can_be_none(self):
        row = self._make_row(display_name=None)
        result = _build_user_out_from_claims(row, jwt_role="user")
        assert result.display_name is None


# ── POST /login route handler (BE-008-U2) ────────────────


from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes import auth_login as _login_mod
from app.db.session import get_db
from app.repositories.user_repository import DuplicateEmailInMirror
from app.services.fusionauth_client import (
    FusionAuthUnavailable,
    FusionAuthValidationError,
)


_FA_USER_ID = "11111111-2222-3333-4444-555555555555"
_FA_TOKEN = "header.payload.signature"
_VALID_PAYLOAD = {
    "email": "user@example.com",
    "password": "password",
}


def _make_user_row(
    user_id: str = _FA_USER_ID,
    email: str = "user@example.com",
    display_name: str | None = "Test User",
    role: str = "user",
) -> MagicMock:
    """Build a stand-in for the User ORM row that the route returns."""
    row = MagicMock()
    row.id = UUID(user_id)
    row.email = email
    row.display_name = display_name
    row.role = role
    return row


def _async_bridge_session(session: MagicMock) -> MagicMock:
    """Attach async-friendly ``run_sync`` / ``commit`` / ``rollback`` to ``session``.

    Post-BA-fix1-1, the login route uses ``await db.run_sync(...)`` and
    ``await db.commit()`` on the mirror auto-create path. A plain
    ``MagicMock`` returns non-awaitable MagicMocks from method calls,
    so we replace those three methods with :class:`AsyncMock` instances.
    ``run_sync`` additionally invokes its callable so the route's lambda
    actually drives the patched ``upsert_user_mirror`` MagicMock
    attached to the module.
    """
    async def _run_sync(fn, *args, **kwargs):
        return fn(session, *args, **kwargs)

    session.run_sync = AsyncMock(side_effect=_run_sync)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _build_client_with_session(session: MagicMock) -> TestClient:
    """Spin up a FastAPI app with just the login router + overridden get_db."""
    _async_bridge_session(session)
    app = FastAPI()
    app.include_router(_login_mod.router, prefix="/api/v1")

    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _install_jwt_validation(
    monkeypatch,
    *,
    claims: dict,
    header: dict | None = None,
) -> None:
    """Patch the BE-006 JWT helpers used by the login route to succeed."""
    monkeypatch.setattr(
        _login_mod,
        "_decode_unverified_header",
        MagicMock(return_value=header or {"alg": "RS256", "kid": "test-kid"}),
    )
    monkeypatch.setattr(
        _login_mod,
        "_verify_jwt_signature_and_claims",
        AsyncMock(return_value=claims),
    )


def _default_claims(
    sub: str = _FA_USER_ID,
    email: str = "user@example.com",
    roles: list[str] | None = None,
    name: str | None = "Test User",
) -> dict:
    claims: dict = {
        "sub": sub,
        "email": email,
        "roles": roles if roles is not None else ["user"],
    }
    if name is not None:
        claims["name"] = name
    return claims


class TestLoginRouteHappyPath:
    """The 200 happy path: FA login → JWT validate → mirror lookup → return."""

    def test_happy_path_user_returns_200_with_token_and_user(self, monkeypatch):
        session = MagicMock()
        user_row = _make_user_row()

        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(monkeypatch, claims=_default_claims())
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=user_row),
        )

        client = _build_client_with_session(session)
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["token"] == _FA_TOKEN
        assert body["user"]["id"] == _FA_USER_ID
        assert body["user"]["email"] == "user@example.com"
        assert body["user"]["display_name"] == "Test User"
        assert body["user"]["role"] == "user"

    def test_happy_path_admin_role_picked_from_jwt(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch,
            claims=_default_claims(
                email="admin@example.com",
                roles=["admin"],
            ),
        )
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(
                return_value=_make_user_row(
                    email="admin@example.com",
                    # mirror's role column is informational only — JWT wins
                    role="user",
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "password"},
        )

        assert resp.status_code == 200
        assert resp.json()["user"]["role"] == "admin"

    def test_role_precedence_super_admin_wins(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch,
            claims=_default_claims(roles=["user", "admin", "super_admin"]),
        )
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=_make_user_row()),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 200
        assert resp.json()["user"]["role"] == "super_admin"

    def test_fa_login_called_with_email_and_password(self, monkeypatch):
        login_mock = AsyncMock(return_value={"token": _FA_TOKEN})
        monkeypatch.setattr(_login_mod.fusionauth_client, "login", login_mock)
        _install_jwt_validation(monkeypatch, claims=_default_claims())
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=_make_user_row()),
        )

        client = _build_client_with_session(MagicMock())
        client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        login_mock.assert_awaited_once()
        kwargs = login_mock.await_args.kwargs
        assert kwargs["email"] == "user@example.com"
        assert kwargs["password"] == "password"


class TestLoginRouteCredentialsFailure:
    """ALL FA 4xx map to identical 401 invalid_credentials (no enumeration)."""

    @pytest.mark.parametrize("fa_status", [400, 401, 403, 404, 410, 423])
    def test_any_fa_4xx_returns_identical_401_invalid_credentials(
        self, monkeypatch, fa_status
    ):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=fa_status,
                    body={"some": "thing"},
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        # CRITICAL — same status, same body, no leakage of which 4xx.
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_credentials"}

    def test_fa_locked_account_does_not_leak_lock_state(self, monkeypatch):
        # 423 locked → still 401 invalid_credentials, same shape.
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=423,
                    body={"reason": "locked"},
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_credentials"}
        # body must NOT echo any FA "locked" wording
        assert "lock" not in resp.text.lower()

    def test_fa_rejection_logged_at_info_with_email_but_no_password(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=404, body={}
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.INFO, logger="app.api.routes.auth_login"):
            client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert any(
            "fa_login_rejected" in rec.getMessage() for rec in caplog.records
        )
        # Password must NEVER appear in logs.
        for rec in caplog.records:
            assert "password" not in rec.getMessage()
            # The 'password' value itself also must not appear.
            assert _VALID_PAYLOAD["password"] not in rec.getMessage()


class TestLoginRouteFaUnavailable:
    """FA 5xx / transport failure → 503 auth_service_unavailable."""

    def test_fa_unavailable_returns_503(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthUnavailable(
                    status_code=None, body=None
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}

    def test_fa_5xx_returns_503(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthUnavailable(
                    status_code=502, body={"err": "bad gateway"}
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}


class TestLoginRouteTokenExtraction:
    """Missing or malformed token in FA response → 502 auth_token_invalid."""

    def test_missing_token_in_fa_response_returns_502(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={}),  # no token key
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 502
        assert resp.json()["detail"] == {"error": "auth_token_invalid"}

    def test_empty_token_string_returns_502(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": ""}),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 502
        assert resp.json()["detail"] == {"error": "auth_token_invalid"}


class TestLoginRouteJwtValidationFailure:
    """JWT helper raises → 502 (or preserve 503 for cold JWKS+FA blip)."""

    def test_invalid_token_from_helper_returns_502(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        monkeypatch.setattr(
            _login_mod,
            "_decode_unverified_header",
            MagicMock(return_value={"alg": "RS256", "kid": "k"}),
        )
        monkeypatch.setattr(
            _login_mod,
            "_verify_jwt_signature_and_claims",
            AsyncMock(
                side_effect=HTTPException(
                    status_code=401, detail={"error": "invalid_token"}
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 502
        assert resp.json()["detail"] == {"error": "auth_token_invalid"}

    def test_expired_token_from_helper_returns_502(self, monkeypatch):
        # FA just issued the token, so an "expired" claim is FA
        # misconfiguration — surface as 502 like other token problems.
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        monkeypatch.setattr(
            _login_mod,
            "_decode_unverified_header",
            MagicMock(return_value={"alg": "RS256", "kid": "k"}),
        )
        monkeypatch.setattr(
            _login_mod,
            "_verify_jwt_signature_and_claims",
            AsyncMock(
                side_effect=HTTPException(
                    status_code=401, detail={"error": "token_expired"}
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 502
        assert resp.json()["detail"] == {"error": "auth_token_invalid"}

    def test_jwks_unavailable_503_is_preserved(self, monkeypatch):
        # Cold-JWKS + FA blip is the one case the helper raises 503 —
        # spec says preserve it because we genuinely cannot validate.
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        monkeypatch.setattr(
            _login_mod,
            "_decode_unverified_header",
            MagicMock(return_value={"alg": "RS256", "kid": "k"}),
        )
        monkeypatch.setattr(
            _login_mod,
            "_verify_jwt_signature_and_claims",
            AsyncMock(
                side_effect=HTTPException(
                    status_code=503,
                    detail={"error": "auth_service_unavailable"},
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}


class TestLoginRouteSubExtraction:
    """Missing or non-UUID sub claim → 502 auth_token_invalid."""

    def test_missing_sub_claim_returns_502(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        claims = _default_claims()
        claims.pop("sub")
        _install_jwt_validation(monkeypatch, claims=claims)

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 502
        assert resp.json()["detail"] == {"error": "auth_token_invalid"}

    def test_non_uuid_sub_returns_502(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch, claims=_default_claims(sub="not-a-uuid")
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 502
        assert resp.json()["detail"] == {"error": "auth_token_invalid"}


class TestLoginRouteRolesGate:
    """Missing / empty / unknown-only roles → 403 no_role_assigned."""

    def test_empty_roles_returns_403(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch, claims=_default_claims(roles=[])
        )
        # get_user_by_id should not even be reached.
        get_mock = AsyncMock()
        monkeypatch.setattr(_login_mod, "get_user_by_id", get_mock)

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 403
        assert resp.json()["detail"] == {"error": "no_role_assigned"}
        get_mock.assert_not_called()

    def test_missing_roles_claim_returns_403(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        claims = _default_claims()
        claims.pop("roles")
        _install_jwt_validation(monkeypatch, claims=claims)

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 403
        assert resp.json()["detail"] == {"error": "no_role_assigned"}

    def test_unknown_roles_only_returns_403(self, monkeypatch):
        # _pick_role returns None when none of the known roles are present.
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch,
            claims=_default_claims(roles=["unknown_role", "another"]),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 403
        assert resp.json()["detail"] == {"error": "no_role_assigned"}


class TestLoginRouteMirrorAutoCreate:
    """Legacy FA users with no local mirror → auto-create from JWT claims."""

    def test_missing_mirror_triggers_upsert_and_commit(self, monkeypatch):
        session = MagicMock()
        new_row = _make_user_row(display_name="Auto Created")

        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(monkeypatch, claims=_default_claims())
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=None),  # no mirror row
        )
        upsert_mock = MagicMock(return_value=new_row)
        monkeypatch.setattr(_login_mod, "upsert_user_mirror", upsert_mock)

        client = _build_client_with_session(session)
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 200
        upsert_mock.assert_called_once()
        kwargs = upsert_mock.call_args.kwargs
        assert kwargs["fa_user_id"] == UUID(_FA_USER_ID)
        assert kwargs["email"] == "user@example.com"
        assert kwargs["role"] == "user"
        assert kwargs["display_name"] == "Test User"
        session.commit.assert_called_once()

    def test_mirror_autocreate_logs_info_with_user_id(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(monkeypatch, claims=_default_claims())
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            _login_mod,
            "upsert_user_mirror",
            MagicMock(return_value=_make_user_row()),
        )

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.INFO, logger="app.api.routes.auth_login"):
            client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert any(
            "mirror_autocreated_on_login" in rec.getMessage()
            for rec in caplog.records
        )

    def test_mirror_autocreate_falls_back_to_payload_email(self, monkeypatch):
        # When the JWT has no email claim, fall back to the payload email.
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        claims = _default_claims()
        claims.pop("email")
        _install_jwt_validation(monkeypatch, claims=claims)
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=None),
        )
        upsert_mock = MagicMock(return_value=_make_user_row())
        monkeypatch.setattr(_login_mod, "upsert_user_mirror", upsert_mock)

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 200
        assert upsert_mock.call_args.kwargs["email"] == "user@example.com"

    def test_mirror_autocreate_uses_preferred_username_when_no_name(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        claims = _default_claims(name=None)
        claims["preferred_username"] = "the_cook"
        _install_jwt_validation(monkeypatch, claims=claims)
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=None),
        )
        upsert_mock = MagicMock(return_value=_make_user_row())
        monkeypatch.setattr(_login_mod, "upsert_user_mirror", upsert_mock)

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 200
        assert upsert_mock.call_args.kwargs["display_name"] == "the_cook"

    def test_duplicate_email_in_mirror_returns_500_duplicate_email_in_mirror(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(monkeypatch, claims=_default_claims())
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            _login_mod,
            "upsert_user_mirror",
            MagicMock(
                side_effect=DuplicateEmailInMirror(
                    email="user@example.com",
                    attempted_id=UUID(_FA_USER_ID),
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 500
        assert resp.json()["detail"] == {"error": "duplicate_email_in_mirror"}


class TestLoginRouteRequestValidation:
    """Pydantic-level validation returns 422 before FA is called."""

    def test_missing_email_returns_422_without_calling_fa(self, monkeypatch):
        login_mock = AsyncMock()
        monkeypatch.setattr(
            _login_mod.fusionauth_client, "login", login_mock
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/login", json={"password": "password"}
        )

        assert resp.status_code == 422
        login_mock.assert_not_called()

    def test_missing_password_returns_422_without_calling_fa(self, monkeypatch):
        login_mock = AsyncMock()
        monkeypatch.setattr(
            _login_mod.fusionauth_client, "login", login_mock
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/login", json={"email": "user@example.com"}
        )

        assert resp.status_code == 422
        login_mock.assert_not_called()

    def test_empty_password_returns_422_without_calling_fa(self, monkeypatch):
        login_mock = AsyncMock()
        monkeypatch.setattr(
            _login_mod.fusionauth_client, "login", login_mock
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "user@example.com", "password": ""},
        )

        assert resp.status_code == 422
        login_mock.assert_not_called()

    def test_malformed_email_returns_422_without_calling_fa(self, monkeypatch):
        login_mock = AsyncMock()
        monkeypatch.setattr(
            _login_mod.fusionauth_client, "login", login_mock
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "not-an-email", "password": "password"},
        )

        assert resp.status_code == 422
        login_mock.assert_not_called()

    def test_mixed_case_email_lowercased_before_fa_call(self, monkeypatch):
        login_mock = AsyncMock(return_value={"token": _FA_TOKEN})
        monkeypatch.setattr(_login_mod.fusionauth_client, "login", login_mock)
        _install_jwt_validation(monkeypatch, claims=_default_claims())
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=_make_user_row()),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "User@Example.COM", "password": "password"},
        )

        assert resp.status_code == 200
        assert login_mock.await_args.kwargs["email"] == "user@example.com"


class TestLoginRoutePasswordNeverLogged:
    """Defense-in-depth: password value must never appear in logs."""

    def test_password_not_in_logs_on_success(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(monkeypatch, claims=_default_claims())
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=_make_user_row()),
        )

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(
            logging.DEBUG, logger="app.api.routes.auth_login"
        ):
            client.post(
                "/api/v1/auth/login",
                json={
                    "email": "user@example.com",
                    "password": "very_secret_value_xyz",
                },
            )

        for rec in caplog.records:
            assert "very_secret_value_xyz" not in rec.getMessage()

    def test_password_not_in_logs_on_fa_unavailable(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthUnavailable(
                    status_code=None, body=None
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(
            logging.DEBUG, logger="app.api.routes.auth_login"
        ):
            client.post(
                "/api/v1/auth/login",
                json={
                    "email": "user@example.com",
                    "password": "another_secret_abc",
                },
            )

        for rec in caplog.records:
            assert "another_secret_abc" not in rec.getMessage()


# ── BE-008-U3 sentinel coverage ──────────────────────────


_SECRET_PASSWORD = "hunter2-secret-xyz"


class TestLoginRouteNoEnumerationLeak:
    """Sentinel — the user-enumeration defense.

    Without this test a future refactor could helpfully distinguish
    "no such user" (FA 404) from "wrong password" (FA 401) and silently
    leak account-existence to unauthenticated callers. The whole point
    of the route is that the two responses are byte-identical.
    """

    def _make_client_for_fa_status(self, monkeypatch, fa_status: int) -> TestClient:
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=fa_status,
                    body={"some": "thing"},
                )
            ),
        )
        return _build_client_with_session(MagicMock())

    def test_unknown_email_and_wrong_password_produce_identical_responses(
        self, monkeypatch
    ):
        # Unknown email (FA 404)
        client_404 = self._make_client_for_fa_status(monkeypatch, 404)
        resp_404 = client_404.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "anything"},
        )

        # Known email + wrong password (FA 401)
        client_401 = self._make_client_for_fa_status(monkeypatch, 401)
        resp_401 = client_401.post(
            "/api/v1/auth/login",
            json={"email": "user@example.com", "password": "wrong"},
        )

        # The load-bearing assertions: identical status, body, and
        # headers (excluding dynamic per-response noise).
        assert resp_404.status_code == resp_401.status_code == 401
        assert resp_404.json() == resp_401.json()
        assert resp_404.json()["detail"] == {"error": "invalid_credentials"}

        # Strip headers known to vary per-response (date, server time).
        def _stable_headers(resp) -> dict:
            blacklist = {"date", "server"}
            return {
                k.lower(): v
                for k, v in resp.headers.items()
                if k.lower() not in blacklist
            }

        assert _stable_headers(resp_404) == _stable_headers(resp_401)

    def test_locked_account_also_identical_to_unknown_email(self, monkeypatch):
        # 423 locked must ALSO look identical so the lock state cannot
        # be inferred from the response shape either.
        client_404 = self._make_client_for_fa_status(monkeypatch, 404)
        resp_404 = client_404.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "anything"},
        )
        client_423 = self._make_client_for_fa_status(monkeypatch, 423)
        resp_423 = client_423.post(
            "/api/v1/auth/login",
            json={"email": "locked@example.com", "password": "anything"},
        )

        assert resp_404.status_code == resp_423.status_code == 401
        assert resp_404.json() == resp_423.json()


class TestLoginRouteMirrorUsesClaimsEmail:
    """When claims['email'] != payload.email, the claims email wins.

    Edge case: FA may canonicalize the email differently from what the
    client posted (case, alias resolution, etc.). The mirror row is a
    snapshot of FA's view, so it MUST use the FA-returned email.
    """

    def test_mirror_autocreate_prefers_claims_email_over_payload_email(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        # claims['email'] differs from the payload email (FA
        # canonicalization scenario).
        _install_jwt_validation(
            monkeypatch,
            claims=_default_claims(email="canonical@example.com"),
        )
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=None),  # forces mirror auto-create
        )
        upsert_mock = MagicMock(
            return_value=_make_user_row(email="canonical@example.com")
        )
        monkeypatch.setattr(_login_mod, "upsert_user_mirror", upsert_mock)

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "user@example.com", "password": "password"},
        )

        assert resp.status_code == 200
        # Claims email — NOT payload email — drives the mirror.
        assert upsert_mock.call_args.kwargs["email"] == "canonical@example.com"


class TestLoginRouteRoleFromJwtNotFromDb:
    """Sentinel matching BE-006-U7 #24: the JWT is authoritative for role.

    If a future refactor accidentally reads role from the mirror row
    instead of the JWT, a stale mirror (e.g. user demoted in FA after
    the mirror was written) would silently grant elevated privileges.
    """

    def test_jwt_role_user_wins_over_stale_db_role_admin(self, monkeypatch):
        # Mirror row carries stale role='admin' (user was admin once,
        # then demoted in FA). The JWT now says only ['user'].
        stale_admin_row = _make_user_row(role="admin")

        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch, claims=_default_claims(roles=["user"])
        )
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=stale_admin_row),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 200
        # JWT wins: user, NOT the mirror's stale 'admin'.
        assert resp.json()["user"]["role"] == "user"


class TestLoginRouteResponseOmitsPassword:
    """Defense-in-depth: the response body must never echo a password key."""

    def _has_password_key(self, obj) -> bool:
        """Walk a nested dict/list and return True if any key == 'password'."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "password":
                    return True
                if self._has_password_key(v):
                    return True
            return False
        if isinstance(obj, list):
            return any(self._has_password_key(item) for item in obj)
        return False

    def test_happy_path_response_has_no_password_key_anywhere(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(monkeypatch, claims=_default_claims())
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=_make_user_row()),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": _SECRET_PASSWORD,
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert not self._has_password_key(body), (
            f"response body must not contain a 'password' key anywhere; "
            f"got {body!r}"
        )
        # And the literal password value must not leak into the body
        # text either (would happen if FA's user echo bled through).
        assert _SECRET_PASSWORD not in resp.text


class TestLoginRoutePasswordNeverLoggedAllBranches:
    """Sentinel — password must not appear in logs on ANY branch.

    Issue spec #22: force every branch (happy + each failure path) and
    assert the literal password value never appears in the captured
    log records (message OR extra dict).
    """

    def _captured_text(self, caplog) -> str:
        """Concatenate every record's message + extras into one blob."""
        parts: list[str] = []
        for rec in caplog.records:
            parts.append(rec.getMessage())
            # Capture every attribute that isn't a standard LogRecord
            # field — those are the keys passed via extra={...}.
            standard = set(logging.LogRecord(
                "x", logging.INFO, "x", 0, "x", None, None
            ).__dict__.keys())
            for k, v in rec.__dict__.items():
                if k not in standard:
                    parts.append(f"{k}={v!r}")
        return "\n".join(parts)

    def _post_with_secret(self, client) -> None:
        client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": _SECRET_PASSWORD,
            },
        )

    def test_happy_path_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(monkeypatch, claims=_default_claims())
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=_make_user_row()),
        )
        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG, logger="app.api.routes.auth_login"):
            self._post_with_secret(client)
        assert _SECRET_PASSWORD not in self._captured_text(caplog)

    def test_fa_4xx_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=401, body={}
                )
            ),
        )
        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG, logger="app.api.routes.auth_login"):
            self._post_with_secret(client)
        assert _SECRET_PASSWORD not in self._captured_text(caplog)

    def test_fa_unavailable_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthUnavailable(
                    status_code=None, body=None
                )
            ),
        )
        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG, logger="app.api.routes.auth_login"):
            self._post_with_secret(client)
        assert _SECRET_PASSWORD not in self._captured_text(caplog)

    def test_missing_token_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={}),  # no token
        )
        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG, logger="app.api.routes.auth_login"):
            self._post_with_secret(client)
        assert _SECRET_PASSWORD not in self._captured_text(caplog)

    def test_invalid_jwt_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        monkeypatch.setattr(
            _login_mod,
            "_decode_unverified_header",
            MagicMock(return_value={"alg": "RS256", "kid": "k"}),
        )
        monkeypatch.setattr(
            _login_mod,
            "_verify_jwt_signature_and_claims",
            AsyncMock(
                side_effect=HTTPException(
                    status_code=401, detail={"error": "invalid_token"}
                )
            ),
        )
        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG, logger="app.api.routes.auth_login"):
            self._post_with_secret(client)
        assert _SECRET_PASSWORD not in self._captured_text(caplog)

    def test_no_role_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch, claims=_default_claims(roles=[])
        )
        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG, logger="app.api.routes.auth_login"):
            self._post_with_secret(client)
        assert _SECRET_PASSWORD not in self._captured_text(caplog)

    def test_mirror_autocreate_branch_redacts_password(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(monkeypatch, claims=_default_claims())
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            _login_mod,
            "upsert_user_mirror",
            MagicMock(return_value=_make_user_row()),
        )
        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG, logger="app.api.routes.auth_login"):
            self._post_with_secret(client)
        assert _SECRET_PASSWORD not in self._captured_text(caplog)
