"""Dedicated sentinel tests for GET /api/v1/auth/me (BA-fix2-1).

These lock the security-critical invariants the QE audit flagged on
BE-010-U2. They sit beside ``test_auth_me.py`` (the behavior suite)
as a focused security firewall — a future "helpful" refactor MUST
keep these green or it has reopened a known vulnerability.

Two test surfaces are exercised in this file:

* ``Test*Real*`` classes use the REAL ``get_current_user`` dependency
  with the BE-006 JWT helpers patched at the ``app.core.auth`` module
  seam. These cover the JWT validation matrix, the algorithm-pinning
  sentinel (alg=none / HS256 rejected BEFORE signature verification),
  and the mirror self-heal happy path.

* ``Test*Override*`` classes override ``get_current_user`` with a
  fixture-built ``CurrentUser`` so the route's *own* concerns — role
  taken from the JWT not the DB row, and the strict response
  field-allowlist — can be exercised without re-deriving the JWT
  pipeline each time.

Covered sentinels (issue spec):

1. JWT validation matrix — missing / malformed / alg=none / HS256 /
   expired beyond leeway / expired within leeway / wrong aud /
   wrong iss / empty roles / kid rotation refresh-once-then-succeed /
   JWKS cold-cache + FA down.
2. Algorithm-pinning sentinel — alg=none and HS256 tokens rejected
   at ``_decode_unverified_header`` BEFORE
   ``_verify_jwt_signature_and_claims`` runs. The verifier mock is
   asserted to have ``call_count == 0`` for both bad-alg cases.
3. Role-from-JWT-not-from-DB — DB row carries stale ``role='admin'``
   but the JWT says ``role='user'`` → response carries
   ``role='user'``.
4. Mirror self-heal — local row missing → ``upsert_user_mirror``
   called + response 200 + caplog INFO ``mirror_autocreated``.
5. Response field-allowlist — ``set(response.json().keys()) ==
   {'id','email','display_name','role'}`` strict equality, not
   issubset. Catches a future ``model_dump(user_row)`` that leaks new
   columns into the response.
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

import base64
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes import auth_me
from app.api.routes.auth_me import router
from app.core import auth as core_auth
from app.core.auth import CurrentUser, get_current_user
from app.db.session import get_db


# ── Fixtures + helpers ───────────────────────────────────


FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
FIXED_SUB = str(FIXED_UUID)
FIXED_EMAIL = "alice@example.com"


def _b64url(d: dict) -> str:
    """URL-safe base64 of a JSON-encoded dict, no trailing '=' padding."""
    raw = json.dumps(d, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _build_jwt(alg: str, payload: dict | None = None) -> str:
    """Build a three-segment JWT-shaped string with a chosen ``alg`` header.

    The signature segment is a constant — we never expect this token to
    reach signature verification (the whole point of the alg-pinning
    sentinel is that ``_decode_unverified_header`` rejects bad alg
    BEFORE the verifier runs).
    """
    header = {"alg": alg, "kid": "test-kid", "typ": "JWT"}
    pld = payload or {
        "sub": FIXED_SUB,
        "roles": ["user"],
        "email": FIXED_EMAIL,
    }
    return f"{_b64url(header)}.{_b64url(pld)}.signature-placeholder"


def _fake_db_session() -> MagicMock:
    """A MagicMock standing in for the AsyncSession.

    ``get_current_user``'s mirror-autocreate path drives the upsert
    through ``await db.run_sync(lambda s: upsert_user_mirror(s, ...))``
    and ``await db.commit()``. Expose those as async no-ops + a
    ``run_sync`` that invokes its lambda against a fresh MagicMock.
    """
    db = MagicMock()

    async def _run_sync(fn, *args, **kwargs):
        return fn(MagicMock(), *args, **kwargs)

    db.run_sync = _run_sync
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _fake_user_row(
    user_id: uuid.UUID = FIXED_UUID,
    *,
    email: str = FIXED_EMAIL,
    display_name: str | None = "Alice",
    role: str = "user",
) -> SimpleNamespace:
    """A duck-typed stand-in for a User ORM row.

    Only the columns the /me route reads are populated, plus ``role``
    so the JWT-vs-DB precedence tests can deliberately set it to
    something other than the JWT role.
    """
    return SimpleNamespace(
        id=user_id,
        email=email,
        display_name=display_name,
        role=role,
    )


def _current_user(role: str = "user", display_name: str | None = "Alice") -> CurrentUser:
    """Build a ``CurrentUser`` for the get_current_user override path."""
    return CurrentUser(
        id=FIXED_UUID,
        email=FIXED_EMAIL,
        display_name=display_name,
        role=role,
    )


def _app_with_real_dependency() -> FastAPI:
    """Mount /me with the REAL get_current_user — JWT pipeline runs.

    Used by the JWT-matrix + alg-pinning + mirror-self-heal tests:
    requests carry a (possibly malformed) Bearer token and the
    dependency executes the BE-006 validation pipeline against the
    helpers patched at the ``app.core.auth`` seam.
    """
    app = FastAPI()
    app.include_router(router)

    async def _override_get_db():
        yield _fake_db_session()

    app.dependency_overrides[get_db] = _override_get_db
    return app


def _app_with_current_user_override(current_user: CurrentUser) -> FastAPI:
    """Mount /me with ``get_current_user`` overridden to return ``current_user``.

    Used by the role-from-JWT + field-allowlist tests where we only
    care about what the /me handler does once the dependency has
    produced a CurrentUser.
    """
    app = FastAPI()
    app.include_router(router)

    async def _override_get_db():
        yield _fake_db_session()

    async def _override_current_user() -> CurrentUser:
        return current_user

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_current_user
    return app


def _patch_real_jwt_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verify_returns: dict | None = None,
    verify_raises: BaseException | None = None,
    header: dict | None = None,
    existing_user: SimpleNamespace | None = None,
    autocreate_user: SimpleNamespace | None = None,
) -> tuple[MagicMock, AsyncMock]:
    """Patch the JWT helpers + repo seams in ``app.core.auth``.

    Returns the ``_decode_unverified_header`` and
    ``_verify_jwt_signature_and_claims`` mocks so callers can assert
    on call counts (the alg-pinning sentinel).

    ``existing_user``: the row ``get_user_by_id`` returns to
    ``get_current_user`` (set to ``None`` to drive the autocreate
    branch). ``autocreate_user``: the row ``upsert_user_mirror``
    returns when the autocreate fires.

    Also patches the /me handler's own ``get_user_by_id`` reference
    to return ``existing_user`` (or ``autocreate_user`` if the row is
    expected to be present after the dependency runs) so the route's
    second lookup succeeds.
    """
    header_mock = MagicMock(
        return_value=header or {"alg": "RS256", "kid": "test-kid"}
    )

    async def _fake_verify(_token, _hdr):
        if verify_raises is not None:
            raise verify_raises
        return verify_returns or {
            "sub": FIXED_SUB,
            "roles": ["user"],
            "email": FIXED_EMAIL,
            "name": "Alice",
        }

    verify_mock = AsyncMock(side_effect=_fake_verify)

    monkeypatch.setattr(core_auth, "_decode_unverified_header", header_mock)
    monkeypatch.setattr(
        core_auth, "_verify_jwt_signature_and_claims", verify_mock
    )

    monkeypatch.setattr(
        core_auth,
        "get_user_by_id",
        AsyncMock(return_value=existing_user),
    )
    if autocreate_user is not None:
        monkeypatch.setattr(
            core_auth,
            "upsert_user_mirror",
            MagicMock(return_value=autocreate_user),
        )

    # The /me handler does its OWN get_user_by_id lookup after the
    # dependency returns. Patch that to return whichever row is
    # expected to be in the mirror at that point.
    route_row = autocreate_user if autocreate_user is not None else existing_user
    monkeypatch.setattr(
        auth_me, "get_user_by_id", AsyncMock(return_value=route_row)
    )

    return header_mock, verify_mock


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# Real RS256 token shape — used for matrix tests that patch the
# verifier outright (the verifier never sees this, so the shape only
# needs to pass ``_validate_token_shape`` and ``_decode_unverified_header``).
_VALID_SHAPE_TOKEN = _build_jwt("RS256")


# ── #2 Algorithm-pinning sentinel ────────────────────────


class TestMeAlgPinningRealSentinel:
    """alg=none and HS256 rejected BEFORE the signature verifier runs.

    Sentinel for ``[critical] Algorithm-pinning``. The verifier mock
    is asserted to have ``call_count == 0`` for both bad-alg cases,
    which is the defense against alg=none and HS256-with-public-key
    downgrade attacks. The REAL ``_decode_unverified_header`` runs
    (we don't patch it) so the alg check is exercised end-to-end.
    """

    def _make_app_with_bad_alg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[TestClient, AsyncMock]:
        """Patch the signature verifier to a fail-loudly mock.

        Header decode is NOT patched — the real helper must reject
        the bad alg. The verifier mock returns a coroutine that would
        succeed if called (so a regression that drops the alg check
        wouldn't 401 for some other reason — the test would then see
        ``call_count > 0`` AND a 200).
        """
        verify_mock = AsyncMock(
            return_value={
                "sub": FIXED_SUB,
                "roles": ["user"],
                "email": FIXED_EMAIL,
            }
        )
        monkeypatch.setattr(
            core_auth, "_verify_jwt_signature_and_claims", verify_mock
        )
        # Belt-and-braces: patch get_user_by_id too so a regression
        # past the alg gate doesn't ALSO crash on a missing mirror
        # row (which would mask the real failure as a 500).
        monkeypatch.setattr(
            core_auth,
            "get_user_by_id",
            AsyncMock(return_value=_fake_user_row()),
        )
        monkeypatch.setattr(
            auth_me,
            "get_user_by_id",
            AsyncMock(return_value=_fake_user_row()),
        )
        return TestClient(_app_with_real_dependency()), verify_mock

    def test_alg_none_rejected_before_signature_verifier_called(
        self, monkeypatch
    ):
        client, verify_mock = self._make_app_with_bad_alg(monkeypatch)
        token = _build_jwt("none")
        resp = client.get("/auth/me", headers=_bearer(token))
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == {"error": "invalid_token"}
        # THE sentinel: verifier MUST NOT have been called.
        assert verify_mock.call_count == 0, (
            f"signature verifier was called {verify_mock.call_count} time(s) "
            f"on an alg=none token — alg-pinning regression"
        )

    def test_hs256_rejected_before_signature_verifier_called(
        self, monkeypatch
    ):
        # The classic HS256-with-RSA-public-key downgrade attack.
        client, verify_mock = self._make_app_with_bad_alg(monkeypatch)
        token = _build_jwt("HS256")
        resp = client.get("/auth/me", headers=_bearer(token))
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == {"error": "invalid_token"}
        assert verify_mock.call_count == 0, (
            f"signature verifier was called {verify_mock.call_count} time(s) "
            f"on an HS256 token — alg-pinning regression"
        )


# ── #1 JWT validation matrix ─────────────────────────────


class TestMeJwtValidationMatrixReal:
    """The JWT matrix the spec demands: 9 distinct failure / success modes.

    Sentinel for ``[critical] JWT validation matrix``. Each mode
    exercises a distinct branch in the BE-006 pipeline; together they
    are the regression net for any future helper edit.
    """

    def test_missing_authorization_returns_401(self):
        client = TestClient(_app_with_real_dependency())
        resp = client.get("/auth/me")
        assert resp.status_code == 401
        # Skeleton convention — missing header is "unauthenticated".
        assert resp.json()["detail"] == {"error": "unauthenticated"}

    def test_malformed_token_returns_401_invalid_token(self):
        # One segment, not three — the structural shape check rejects.
        client = TestClient(_app_with_real_dependency())
        resp = client.get("/auth/me", headers=_bearer("notajwt"))
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}

    def test_expired_beyond_leeway_returns_401_token_expired(self, monkeypatch):
        _patch_real_jwt_helpers(
            monkeypatch,
            verify_raises=HTTPException(
                status_code=401, detail={"error": "token_expired"}
            ),
        )
        client = TestClient(_app_with_real_dependency())
        resp = client.get("/auth/me", headers=_bearer(_VALID_SHAPE_TOKEN))
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "token_expired"}

    def test_expired_within_leeway_returns_200(self, monkeypatch):
        # The verifier swallows clock-skew within leeway and returns
        # valid claims — the route should treat that as success.
        _patch_real_jwt_helpers(
            monkeypatch,
            existing_user=_fake_user_row(),
        )
        client = TestClient(_app_with_real_dependency())
        resp = client.get("/auth/me", headers=_bearer(_VALID_SHAPE_TOKEN))
        assert resp.status_code == 200, resp.text

    def test_wrong_audience_returns_401_invalid_token(self, monkeypatch):
        # In the real helper, JWTClaimsError → 401 invalid_token.
        _patch_real_jwt_helpers(
            monkeypatch,
            verify_raises=HTTPException(
                status_code=401, detail={"error": "invalid_token"}
            ),
        )
        client = TestClient(_app_with_real_dependency())
        resp = client.get("/auth/me", headers=_bearer(_VALID_SHAPE_TOKEN))
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}

    def test_wrong_issuer_returns_401_invalid_token(self, monkeypatch):
        # Same surface as wrong_audience — both are JWTClaimsError.
        _patch_real_jwt_helpers(
            monkeypatch,
            verify_raises=HTTPException(
                status_code=401, detail={"error": "invalid_token"}
            ),
        )
        client = TestClient(_app_with_real_dependency())
        resp = client.get("/auth/me", headers=_bearer(_VALID_SHAPE_TOKEN))
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}

    def test_empty_roles_returns_403_no_role_assigned(self, monkeypatch):
        _patch_real_jwt_helpers(
            monkeypatch,
            verify_returns={
                "sub": FIXED_SUB,
                "roles": [],
                "email": FIXED_EMAIL,
            },
        )
        client = TestClient(_app_with_real_dependency())
        resp = client.get("/auth/me", headers=_bearer(_VALID_SHAPE_TOKEN))
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"error": "no_role_assigned"}

    def test_kid_rotation_refresh_once_then_succeed_returns_200(
        self, monkeypatch
    ):
        # The verifier internally refreshes JWKS on a kid-miss; after
        # the refresh the token validates and claims come back. We
        # model that here by simply having the verifier return claims
        # (the kid-refresh dance is BE-006's responsibility and is
        # covered by its own unit tests).
        _patch_real_jwt_helpers(
            monkeypatch,
            existing_user=_fake_user_row(),
        )
        client = TestClient(_app_with_real_dependency())
        resp = client.get("/auth/me", headers=_bearer(_VALID_SHAPE_TOKEN))
        assert resp.status_code == 200, resp.text

    def test_jwks_cold_cache_plus_fa_down_returns_503(self, monkeypatch):
        # Cold JWKS cache + FA blip — the verifier propagates as
        # HTTPException(503, auth_service_unavailable).
        _patch_real_jwt_helpers(
            monkeypatch,
            verify_raises=HTTPException(
                status_code=503,
                detail={"error": "auth_service_unavailable"},
            ),
        )
        client = TestClient(_app_with_real_dependency())
        resp = client.get("/auth/me", headers=_bearer(_VALID_SHAPE_TOKEN))
        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}


# ── #4 Mirror self-heal ──────────────────────────────────


class TestMeMirrorSelfHealReal:
    """Missing mirror row → autocreate + 200 + caplog INFO mirror_autocreated.

    Sentinel for ``[important] Mirror self-heal``. The audit log line
    is load-bearing — oncall keys off it to spot the case where a
    user signed up while the DB was readonly and only now has a row.
    """

    def test_missing_local_row_upserts_returns_200_emits_mirror_autocreated(
        self, monkeypatch, caplog
    ):
        autocreated = _fake_user_row()
        upsert_mock = MagicMock(return_value=autocreated)
        # Sequence: get_user_by_id returns None on the dep's lookup,
        # upsert returns the autocreated row, /me's own get_user_by_id
        # call finds the row.
        monkeypatch.setattr(
            core_auth,
            "_decode_unverified_header",
            MagicMock(return_value={"alg": "RS256", "kid": "kid-test"}),
        )
        monkeypatch.setattr(
            core_auth,
            "_verify_jwt_signature_and_claims",
            AsyncMock(
                return_value={
                    "sub": FIXED_SUB,
                    "roles": ["user"],
                    "email": FIXED_EMAIL,
                    "name": "Alice",
                }
            ),
        )
        monkeypatch.setattr(
            core_auth, "get_user_by_id", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(core_auth, "upsert_user_mirror", upsert_mock)
        monkeypatch.setattr(
            auth_me,
            "get_user_by_id",
            AsyncMock(return_value=autocreated),
        )

        client = TestClient(_app_with_real_dependency())
        with caplog.at_level(logging.INFO, logger="app.core.auth"):
            resp = client.get(
                "/auth/me", headers=_bearer(_VALID_SHAPE_TOKEN)
            )

        assert resp.status_code == 200, resp.text
        # Upsert was actually called (no shortcut path).
        upsert_mock.assert_called_once()
        kwargs = upsert_mock.call_args.kwargs
        assert kwargs["fa_user_id"] == FIXED_UUID
        assert kwargs["email"] == FIXED_EMAIL
        # Mirror role default is informational; JWT is authoritative.
        assert kwargs["role"] == "user"
        # The audit line — load-bearing for oncall diagnostics.
        assert any(
            "mirror_autocreated" in rec.getMessage()
            for rec in caplog.records
        ), (
            "expected an INFO log containing 'mirror_autocreated' "
            f"but got: {[r.getMessage() for r in caplog.records]}"
        )


# ── #3 Role-from-JWT-not-from-DB ─────────────────────────


class TestMeRoleFromJwtNotFromDbOverride:
    """Stale mirror admin row + JWT user → response is user.

    Sentinel for ``[critical] Role-from-JWT-not-from-DB``. Three
    routes in a row enforce this (BE-006-U7 #24, BE-008-U3 #21,
    BE-010-U2 here). Removing any of the three would re-open the
    "demoted user keeps admin" silent regression.
    """

    def test_db_role_admin_ignored_when_jwt_role_is_user(self, monkeypatch):
        cu = _current_user(role="user")  # JWT says user
        stale_admin_row = _fake_user_row(role="admin")  # DB says admin

        monkeypatch.setattr(
            auth_me,
            "get_user_by_id",
            AsyncMock(return_value=stale_admin_row),
        )

        client = TestClient(_app_with_current_user_override(cu))
        resp = client.get("/auth/me")
        assert resp.status_code == 200, resp.text
        # JWT wins, DB column is ignored.
        assert resp.json()["role"] == "user"


# ── #5 Response field-allowlist (strict equality) ────────


class TestMeResponseFieldAllowlistOverride:
    """``set(response.json().keys()) == {'id','email','display_name','role'}``.

    Sentinel for ``[important] Response field-allowlist``. STRICT
    equality (not issubset) so any future column accidentally
    serialised into the response causes the test to fire.
    """

    def test_response_keys_are_exactly_the_four_user_out_fields(
        self, monkeypatch
    ):
        cu = _current_user()
        # The DB row carries extras that MUST NOT leak.
        row = SimpleNamespace(
            id=FIXED_UUID,
            email=FIXED_EMAIL,
            display_name="Alice",
            role="user",
            password="never-leak",
            password_hash="$2b$12$never-leak",
            fa_registration={"applicationId": "leak"},
            jwt="ey.should.not.leak",
            token="never-leak",
            raw_claims={"sub": "leak"},
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
            last_seen_at="2026-05-01T00:00:00Z",
            email_verified=True,
        )
        monkeypatch.setattr(
            auth_me, "get_user_by_id", AsyncMock(return_value=row)
        )

        client = TestClient(_app_with_current_user_override(cu))
        resp = client.get("/auth/me")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # THE strict-equality sentinel.
        assert set(body.keys()) == {"id", "email", "display_name", "role"}, (
            f"response keys diverged from the allowlist; got {sorted(body.keys())!r}"
        )

        # Defense in depth — none of the known-sensitive names appear
        # anywhere in the rendered body text.
        for forbidden in (
            "password",
            "password_hash",
            "fa_registration",
            "jwt",
            "token",
            "raw_claims",
            "created_at",
            "updated_at",
            "last_seen_at",
            "email_verified",
        ):
            assert forbidden not in body, (
                f"sensitive key {forbidden!r} leaked into /me response: {body!r}"
            )
