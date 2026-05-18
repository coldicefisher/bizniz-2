"""Dedicated sentinel tests for POST /api/v1/auth/login (BA-fix2-1).

These lock the security-critical invariants the QE audit flagged on
BE-008-U3. They sit beside ``test_auth_login.py`` (the behavior suite)
as a focused security firewall — a future refactor MUST keep these
green or it has reopened a known vulnerability.

Covered sentinels:

1. Anti-enumeration — FA 404 (unknown user), FA 401 (wrong password),
   and FA 423 (locked) ALL produce a BYTE-IDENTICAL 401 body
   ``{error:'invalid_credentials'}``. Checked via ``response.content``
   equality so a future "helpful" body tweak that distinguishes the
   three is caught.
2. Role precedence — ``roles=['user','admin']`` → ``role='admin'``;
   ``roles=['user','admin','super_admin']`` → ``role='super_admin'``.
3. Role-from-JWT-not-from-DB — mirror row carries stale ``role='admin'``
   but the JWT claims only ``roles=['user']`` → response carries
   ``role='user'``. Without this sentinel a future regression to
   ``user_row.role`` would silently grant elevated privileges.
4. FA-unavailable — ``FusionAuthUnavailable`` from ``login`` → 503
   ``auth_service_unavailable`` AND the JWT-validation helpers are
   NEVER called (no wasted JWKS fetches when FA blew up).
5. Invalid-JWT-from-FA — verifier raises a non-503 HTTPException →
   502 ``auth_token_invalid`` (NOT 401, NOT 503: FA misconfiguration,
   not a user-credential problem).
6. Password-never-logged — sentinel password absent across happy +
   401 + 503 + 502 branches in every record's message, extras, and
   exc repr.
7. Empty/no-role-assigned — JWT with empty/unknown roles → 403
   ``no_role_assigned``.
"""
# Settings() needs the three FA env vars at import time.
import os as _os

_os.environ.setdefault(
    "FUSIONAUTH_TENANT_ID", "d4465dd9-12e7-4715-bc4e-690874974b6b"
)
_os.environ.setdefault(
    "FUSIONAUTH_APPLICATION_ID", "85a03867-dccf-4882-adde-1a79aeec50df"
)
_os.environ.setdefault("FUSIONAUTH_API_KEY", "test-api-key-xyz")

import logging
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes import auth_login as _login_mod
from app.db.session import get_db
from app.services.fusionauth_client import (
    FusionAuthUnavailable,
    FusionAuthValidationError,
)


_FA_USER_ID = "11111111-2222-3333-4444-555555555555"
_FA_TOKEN = "header.payload.signature"
_SENTINEL_PASSWORD = "Hunter2-S3cret!"
_PAYLOAD = {
    "email": "user@example.com",
    "password": _SENTINEL_PASSWORD,
}


def _make_user_row(
    user_id: str = _FA_USER_ID,
    email: str = "user@example.com",
    display_name: str | None = "Test User",
    role: str = "user",
) -> MagicMock:
    row = MagicMock()
    row.id = UUID(user_id)
    row.email = email
    row.display_name = display_name
    row.role = role
    return row


def _async_bridge_session(session: MagicMock) -> MagicMock:
    async def _run_sync(fn, *args, **kwargs):
        return fn(session, *args, **kwargs)

    session.run_sync = AsyncMock(side_effect=_run_sync)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _client(session: MagicMock | None = None) -> TestClient:
    sess = _async_bridge_session(session or MagicMock())
    app = FastAPI()
    app.include_router(_login_mod.router, prefix="/api/v1")

    async def _override_get_db():
        yield sess

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


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


def _install_jwt_validation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claims: dict,
    header: dict | None = None,
) -> tuple[MagicMock, AsyncMock]:
    """Patch the BE-006 JWT helpers used by the login route.

    Returns the patched mocks so the caller can assert call_count.
    """
    header_mock = MagicMock(
        return_value=header or {"alg": "RS256", "kid": "test-kid"}
    )
    verify_mock = AsyncMock(return_value=claims)
    monkeypatch.setattr(_login_mod, "_decode_unverified_header", header_mock)
    monkeypatch.setattr(
        _login_mod, "_verify_jwt_signature_and_claims", verify_mock
    )
    return header_mock, verify_mock


def _captured_text_blob(caplog: pytest.LogCaptureFixture) -> str:
    """Concatenate every log record's message + extras + repr(msg)."""
    standard = set(
        logging.LogRecord(
            "x", logging.INFO, "x", 0, "x", None, None
        ).__dict__.keys()
    )
    parts: list[str] = []
    for rec in caplog.records:
        try:
            parts.append(rec.getMessage())
        except Exception:
            pass
        parts.append(repr(rec.msg))
        if rec.exc_text:
            parts.append(rec.exc_text)
        for k, v in rec.__dict__.items():
            if k in standard:
                continue
            parts.append(f"{k}={v!r}")
    return "\n".join(parts)


# ── #1 Anti-enumeration: byte-identical 401 across FA 404/401/423 ──


class TestLoginAntiEnumerationSentinel:
    """FA 404/401/423 MUST collapse to byte-identical 401 responses.

    Sentinel for ``[critical] Anti-enumeration``. Distinguishing
    "unknown user" / "wrong password" / "locked account" in the
    response shape leaks account-existence + lock state to
    unauthenticated callers.
    """

    def _post_with_fa_status(self, fa_status: int, monkeypatch) -> bytes:
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=fa_status, body={"k": fa_status}
                )
            ),
        )
        resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 401, (
            f"FA {fa_status} should map to 401 but got {resp.status_code}: "
            f"{resp.text}"
        )
        return resp.content

    def test_fa_404_401_423_all_return_byte_identical_401(self, monkeypatch):
        body_404 = self._post_with_fa_status(404, monkeypatch)
        body_401 = self._post_with_fa_status(401, monkeypatch)
        body_423 = self._post_with_fa_status(423, monkeypatch)

        # Strict bytes equality — no per-status detail allowed through.
        assert body_404 == body_401 == body_423, (
            f"FA-status responses diverged:\n"
            f"  404 body={body_404!r}\n"
            f"  401 body={body_401!r}\n"
            f"  423 body={body_423!r}"
        )
        # And the shared body is the documented invalid_credentials envelope.
        import json
        assert json.loads(body_404) == {
            "detail": {"error": "invalid_credentials"}
        }


# ── #2 Role precedence ───────────────────────────────────


class TestLoginRolePrecedenceSentinel:
    """``_pick_role`` precedence is end-to-end: admin > user, super > admin.

    Sentinel for ``[important] Role precedence``. Locks the response
    carries the highest-privilege role present in the JWT, never the
    first or last in the list.
    """

    def test_user_plus_admin_yields_role_admin(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch,
            claims=_default_claims(roles=["user", "admin"]),
        )
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=_make_user_row(role="user")),
        )

        resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 200, resp.text
        assert resp.json()["user"]["role"] == "admin"

    def test_user_plus_admin_plus_super_admin_yields_super_admin(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch,
            claims=_default_claims(
                roles=["user", "admin", "super_admin"]
            ),
        )
        monkeypatch.setattr(
            _login_mod,
            "get_user_by_id",
            AsyncMock(return_value=_make_user_row(role="user")),
        )

        resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 200, resp.text
        assert resp.json()["user"]["role"] == "super_admin"


# ── #3 Role-from-JWT-not-from-DB ─────────────────────────


class TestLoginRoleFromJwtNotFromDbSentinel:
    """Mirror row says admin, JWT says user → response says user.

    Sentinel for ``[critical] Role-from-JWT-not-from-DB``. A future
    regression that reads ``user_row.role`` would silently grant
    elevated privileges to demoted-but-not-yet-mirror-updated users.
    """

    def test_stale_db_admin_is_ignored_when_jwt_says_user(self, monkeypatch):
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

        resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 200, resp.text
        # JWT wins — DB's stale 'admin' MUST NOT bleed through.
        assert resp.json()["user"]["role"] == "user"


# ── #4 FA-unavailable 503 + JWT validator NOT called ─────


class TestLoginFaUnavailableSentinel:
    """FA login blowing up MUST short-circuit before JWT validation.

    Sentinel for ``[important] FA-unavailable mapping``. The negative
    assertion (validator never reached) catches a future "let's
    validate the token we never got" bug that would 502 instead of
    503'ing.
    """

    def test_fa_unavailable_returns_503_and_jwt_validator_not_called(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthUnavailable(
                    status_code=None, body=None
                )
            ),
        )
        header_mock = MagicMock()
        verify_mock = AsyncMock()
        monkeypatch.setattr(
            _login_mod, "_decode_unverified_header", header_mock
        )
        monkeypatch.setattr(
            _login_mod, "_verify_jwt_signature_and_claims", verify_mock
        )

        resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)

        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}
        # The load-bearing negative assertion — neither helper was reached.
        assert header_mock.call_count == 0
        assert verify_mock.call_count == 0


# ── #5 Invalid-JWT-from-FA → 502 auth_token_invalid ──────


class TestLoginInvalidJwtFromFaSentinel:
    """FA-returned JWT that fails our validator → 502, never 401/503.

    Sentinel for ``[important] Invalid-JWT-from-FA mapping``. FA just
    issued the token, so a rejection means FA misconfiguration — NOT
    a user credential failure (401) and NOT a service blip (503).
    """

    def test_invalid_token_from_validator_yields_502_auth_token_invalid(
        self, monkeypatch
    ):
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

        resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)

        assert resp.status_code == 502
        assert resp.json()["detail"] == {"error": "auth_token_invalid"}

    def test_validator_503_is_preserved_as_503(self, monkeypatch):
        # Single carve-out — cold JWKS + FA blip MUST stay 503.
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

        resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}


# ── #6 Empty roles → 403 no_role_assigned ────────────────


class TestLoginEmptyRolesSentinel:
    """Empty / unknown-only roles → 403 no_role_assigned.

    Sentinel for ``[important] Empty/no-role-assigned 403``. The point
    is that a valid JWT with no Recipe-Box roles is a 403 (you have a
    credential but no entitlement), not a 401 (no/bad credential) or
    a 200 with role=null.
    """

    def test_empty_roles_returns_403(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch, claims=_default_claims(roles=[])
        )

        resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"error": "no_role_assigned"}

    def test_only_unknown_roles_returns_403(self, monkeypatch):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch,
            claims=_default_claims(roles=["random_role", "viewer"]),
        )

        resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"error": "no_role_assigned"}


# ── #7 Password-never-logged across all four branches ────


class TestLoginPasswordNeverLoggedAllBranches:
    """Submitted password absent from logs on happy + 401 + 503 + 502.

    Sentinel for ``[critical] Password-never-logged sentinel for login
    across happy + 401 + 503 + 502 branches``.
    """

    def _assert_password_absent(self, caplog: pytest.LogCaptureFixture) -> None:
        blob = _captured_text_blob(caplog)
        assert _SENTINEL_PASSWORD not in blob, (
            f"sentinel password leaked into logs:\n{blob}"
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
        with caplog.at_level(logging.DEBUG):
            resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 200, resp.text
        self._assert_password_absent(caplog)

    def test_401_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=401, body={}
                )
            ),
        )
        with caplog.at_level(logging.DEBUG):
            resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 401
        self._assert_password_absent(caplog)

    def test_503_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(
                side_effect=FusionAuthUnavailable(
                    status_code=None, body=None
                )
            ),
        )
        with caplog.at_level(logging.DEBUG):
            resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 503
        self._assert_password_absent(caplog)

    def test_502_branch_redacts_password(self, monkeypatch, caplog):
        # Invalid JWT from FA → 502 path
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
        with caplog.at_level(logging.DEBUG):
            resp = _client().post("/api/v1/auth/login", json=_PAYLOAD)
        assert resp.status_code == 502
        self._assert_password_absent(caplog)
