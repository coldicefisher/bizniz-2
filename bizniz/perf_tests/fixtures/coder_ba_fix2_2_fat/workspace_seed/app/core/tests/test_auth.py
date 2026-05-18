"""Unit tests for the auth dependency (BE-006-U7).

Covers every error path of ``get_current_user`` and ``require_roles``
specified in BE-006:

  * Authorization-header shape failures.
  * Algorithm pinning (alg=none and HS256 are both 401 invalid_token).
  * JWKS kid-rotation: refresh-once-and-retry contract.
  * JWKS unavailability — cold cache → 503; warm cache → 401.
  * Claim validation — exp / nbf / aud / iss.
  * Role precedence (super_admin > admin > user) read from JWT only.
  * Local mirror auto-create + the duplicate-email collision case.
  * require_roles allow / reject.
  * The load-bearing invariant: role comes from the JWT, not from
    the local mirror's ``role`` column.

Tokens are minted with a real RSA keypair per session (~100ms,
amortized) and the matching JWK is monkeypatched into
``fusionauth_client.get_jwks`` so the file never touches a live
FusionAuth or Postgres.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from typing import Any

# Settings() requires these to instantiate; the container env may not
# carry FUSIONAUTH_TENANT_ID. ``setdefault`` so any real env wins.
os.environ.setdefault(
    "FUSIONAUTH_TENANT_ID", "00000000-0000-0000-0000-000000000000"
)
os.environ.setdefault("FUSIONAUTH_APPLICATION_ID", "test-app")
os.environ.setdefault("FUSIONAUTH_API_KEY", "test-api-key-xyz")
os.environ.setdefault("FUSIONAUTH_ISSUER", "acme.com")

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from jose import jwt
from jose.utils import base64url_encode

from app.core import auth as auth_module
from app.core.auth import CurrentUser, get_current_user, require_roles
from app.db.session import get_db
from app.repositories.user_repository import DuplicateEmailInMirror
from app.services.fusionauth_client import FusionAuthUnavailable


TEST_KID = "kid-test"
OTHER_KID = "kid-other"
TEST_SUB = "11111111-1111-1111-1111-111111111111"
TEST_EMAIL = "alice@example.com"


# ── Cryptographic helpers ───────────────────────────────────


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[Any, str]:
    """Generate one RSA keypair for the test session.

    Returns ``(public_key_obj, private_pem)``. RSA generation is the
    slow part (~100ms); session scope amortizes the cost across all
    24 tests in this file.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return private_key.public_key(), pem


def _build_jwk(public_key: Any, kid: str) -> dict:
    """Encode an RSA public key as a JWK dict (matches what FA publishes)."""
    numbers = public_key.public_numbers()
    n_bytes = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e_bytes = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {
        "kid": kid,
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "n": base64url_encode(n_bytes).decode("ascii"),
        "e": base64url_encode(e_bytes).decode("ascii"),
    }


@pytest.fixture(scope="session")
def test_jwk(rsa_keypair: tuple[Any, str]) -> dict:
    """The JWK matching the session-scoped RSA keypair, kid=TEST_KID."""
    public_key, _ = rsa_keypair
    return _build_jwk(public_key, TEST_KID)


def _default_claims(**overrides: Any) -> dict:
    """Build a valid claim set defaulting to ``test-app`` / ``acme.com``."""
    now = int(time.time())
    base = {
        "sub": TEST_SUB,
        "email": TEST_EMAIL,
        "roles": ["user"],
        "iss": "acme.com",
        "aud": "test-app",
        "iat": now,
        "exp": now + 3600,
    }
    base.update(overrides)
    return base


def _mint_rs256(
    private_pem: str,
    claims: dict | None = None,
    kid: str = TEST_KID,
) -> str:
    """Sign a real RS256 JWT with the session RSA private key."""
    return jwt.encode(
        claims if claims is not None else _default_claims(),
        private_pem,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _mint_hs256(claims: dict | None = None, kid: str = TEST_KID) -> str:
    """Sign an HS256 token. The alg-pin must reject before signature check."""
    return jwt.encode(
        claims if claims is not None else _default_claims(),
        "shared-secret-doesnt-matter",
        algorithm="HS256",
        headers={"kid": kid},
    )


def _alg_none_token(claims: dict | None = None, kid: str = TEST_KID) -> str:
    """Craft an alg='none' token by hand (python-jose refuses to mint these).

    Layout: ``base64url(header).base64url(payload).`` — three segments
    with empty signature. Defeats the alg-pin only if the validator
    doesn't pre-check the header.
    """
    header = {"alg": "none", "typ": "JWT", "kid": kid}
    payload = claims if claims is not None else _default_claims()
    h_b64 = (
        base64.urlsafe_b64encode(
            json.dumps(header, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    p_b64 = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    return f"{h_b64}.{p_b64}."


# ── DB / repo fakes ─────────────────────────────────────────


class _FakeUser:
    """Stand-in for the SQLAlchemy ``User`` row.

    Exposes only the attributes ``get_current_user`` reads off the
    row (``email`` and ``display_name``). ``role`` is set so the
    "role from JWT not from DB" test can prove the DB column is
    being ignored.
    """

    def __init__(
        self,
        id: uuid.UUID,
        email: str = TEST_EMAIL,
        display_name: str | None = "Alice",
        role: str = "user",
    ) -> None:
        self.id = id
        self.email = email
        self.display_name = display_name
        self.role = role


class _FakeDB:
    """Minimal AsyncSession stand-in supporting the BA-fix1-1 contract.

    Post-repair ``get_current_user`` bridges its synchronous mirror
    upsert through ``await db.run_sync(lambda s: ...)``; ``run_sync``
    invokes the lambda with a synthetic sync session and returns its
    value. ``commit`` and ``rollback`` are async no-ops that flip
    booleans tests can assert on.
    """

    def __init__(self) -> None:
        self.commit_called = False
        self.rollback_called = False

    async def run_sync(self, fn, *args, **kwargs):
        return fn(self, *args, **kwargs)

    async def commit(self) -> None:
        self.commit_called = True

    async def rollback(self) -> None:
        self.rollback_called = True


# ── Patch helpers ───────────────────────────────────────────


def _patch_jwks_calls(
    monkeypatch: pytest.MonkeyPatch, results: list[Any]
) -> list[Any]:
    """Patch ``fusionauth_client.get_jwks`` with a side-effect queue.

    Each entry is either a dict (returned) or an Exception (raised).
    Over-fetching past the queue raises AssertionError so the
    "more HTTP calls than expected" regression is caught. The
    returned list logs each call so tests assert on count.
    """
    pending = list(results)
    call_log: list[Any] = []

    async def _fake_get_jwks():
        call_log.append(True)
        if not pending:
            raise AssertionError(
                "fusionauth_client.get_jwks called more times than expected"
            )
        result = pending.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        auth_module.fusionauth_client, "get_jwks", _fake_get_jwks
    )
    return call_log


def _patch_repo(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing_user: Any = None,
    upsert_result: Any = None,
    upsert_raises: BaseException | None = None,
) -> dict:
    """Patch repo functions at the auth module seam. Returns capture dict."""
    captured: dict = {"upsert_called": False, "kwargs": {}}

    async def _fake_get_user_by_id(session, user_id):
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


# ── App / client fixtures ───────────────────────────────────


@pytest.fixture
def app() -> FastAPI:
    """Minimal FastAPI app with ``/me`` and ``/admin`` routes.

    Both routes return ``model_dump(mode='json')`` so tests can assert
    on role precedence without serialising the Pydantic model manually.
    """
    app = FastAPI()

    @app.get("/me")
    async def me(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """Return the authenticated caller's CurrentUser as JSON."""
        return current_user.model_dump(mode="json")

    @app.get("/admin")
    async def admin(
        current_user: CurrentUser = Depends(require_roles(["admin"])),
    ) -> dict:
        """Admin-gated route — exercises ``require_roles``."""
        return current_user.model_dump(mode="json")

    async def _fake_db_dep():
        yield _FakeDB()

    app.dependency_overrides[get_db] = _fake_db_dep
    return app


@pytest.fixture
async def client(app: FastAPI):
    """Async ASGI client against the in-process test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _reset_auth_state(monkeypatch: pytest.MonkeyPatch):
    """Hermetic auth state for every test.

    * JWKS cache cleared before and after each test.
    * ``_jwks_lock`` rebound to a fresh asyncio.Lock so it lazy-binds
      to the test's own event loop (pytest-asyncio uses function-scope
      loops by default — a stale lock raises "bound to a different
      event loop" on the next acquire).
    * Settings overridden so the assertions don't drift with whatever
      env vars the container happens to carry: ``test-app``,
      ``acme.com``, 60s leeway.
    """
    auth_module._reset_jwks_cache_for_tests()
    auth_module._jwks_lock = asyncio.Lock()
    monkeypatch.setattr(
        auth_module.settings, "fusionauth_application_id", "test-app"
    )
    monkeypatch.setattr(auth_module.settings, "fusionauth_issuer", "acme.com")
    monkeypatch.setattr(auth_module.settings, "jwt_leeway_seconds", 60)
    yield
    auth_module._reset_jwks_cache_for_tests()


# ── 1-4: Authorization header shape ─────────────────────────


@pytest.mark.unit
class TestAuthorizationHeaderShape:
    async def test_missing_authorization_header_returns_401(self, client):
        resp = await client.get("/me")
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "unauthenticated"}

    async def test_non_bearer_scheme_returns_401_unauthenticated(self, client):
        resp = await client.get(
            "/me", headers={"Authorization": "Basic abc123"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "unauthenticated"}

    async def test_empty_token_returns_401_unauthenticated(self, client):
        resp = await client.get("/me", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "unauthenticated"}

    async def test_malformed_token_two_segments_returns_401_invalid_token(
        self, client
    ):
        resp = await client.get(
            "/me", headers={"Authorization": "Bearer abc.def"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}


# ── 5-6: Algorithm pinning ──────────────────────────────────


@pytest.mark.unit
class TestAlgorithmPin:
    async def test_alg_none_token_returns_401_invalid_token(
        self, client, caplog
    ):
        token = _alg_none_token()
        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            resp = await client.get(
                "/me", headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}
        # WARN log surfaces the unexpected alg for ops debugging.
        assert any(
            "unexpected_alg" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    async def test_hs256_token_returns_401_invalid_token(self, client):
        # Even a token whose body would happily validate under a shared
        # secret must be rejected at the alg-pin step BEFORE signature
        # verification. Defeats the public-key-as-HS256-secret attack.
        token = _mint_hs256()
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}


# ── 7-10: JWKS / kid rotation / availability ────────────────


@pytest.mark.unit
class TestJwksRotation:
    async def test_kid_miss_triggers_one_refresh_then_succeeds(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        # 1st get_jwks call (cold-cache warm-up) returns empty keys.
        # 2nd get_jwks call (refresh after kid miss) returns the real
        # JWK that matches our test token. End state: 200.
        _, pem = rsa_keypair
        call_log = _patch_jwks_calls(
            monkeypatch, [{"keys": []}, {"keys": [test_jwk]}]
        )
        _patch_repo(
            monkeypatch,
            existing_user=_FakeUser(uuid.UUID(TEST_SUB), role="user"),
        )

        # Warm the cache with the empty-keys "first response".
        await auth_module._get_jwks_with_refresh("warm-up-kid")
        assert len(call_log) == 1

        token = _mint_rs256(pem, _default_claims())
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )

        assert resp.status_code == 200, resp.text
        # Exactly two HTTP calls — warm-up + the one refresh.
        assert len(call_log) == 2

    async def test_kid_miss_with_no_match_after_refresh_returns_401_invalid_token(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        # Both responses carry only OTHER_KID — neither contains
        # TEST_KID, so the post-refresh kid lookup still fails.
        _, pem = rsa_keypair
        other_jwk = dict(test_jwk, kid=OTHER_KID)
        call_log = _patch_jwks_calls(
            monkeypatch, [{"keys": [other_jwk]}, {"keys": [other_jwk]}]
        )
        _patch_repo(monkeypatch)

        # Warm cache (1st call): cache populated with OTHER_KID only.
        await auth_module._get_jwks_with_refresh("warm-up-kid")
        assert len(call_log) == 1

        token = _mint_rs256(pem, _default_claims())
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )

        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}
        # Exactly two calls — warm-up + the one (still-failing) refresh.
        assert len(call_log) == 2

    async def test_jwks_unavailable_cold_cache_returns_503(
        self, client, monkeypatch, rsa_keypair
    ):
        # Cold cache + FA down → no fallback keys to even attempt
        # verification with → translate to 503.
        _, pem = rsa_keypair
        _patch_jwks_calls(
            monkeypatch,
            [FusionAuthUnavailable(status_code=None, body=None, message="down")],
        )
        _patch_repo(monkeypatch)
        assert auth_module._jwks_cache is None

        token = _mint_rs256(pem, _default_claims())
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}

    async def test_jwks_unavailable_warm_cache_falls_through(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        # Warm cache lacks the kid; FA blips during the refresh.
        # Helper swallows + returns stale cache → verify can't match
        # kid → 401 invalid_token (NOT 503).
        _, pem = rsa_keypair
        other_jwk = dict(test_jwk, kid=OTHER_KID)
        auth_module._jwks_cache = {"keys": [other_jwk]}
        _patch_jwks_calls(
            monkeypatch,
            [FusionAuthUnavailable(status_code=None, body=None, message="blip")],
        )
        _patch_repo(monkeypatch)

        token = _mint_rs256(pem, _default_claims())
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}


# ── 11-15: Claim validation ─────────────────────────────────


@pytest.mark.unit
class TestClaims:
    async def test_expired_token_returns_401_token_expired(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        # exp = now-300 (5 min ago), leeway = 60s → outside leeway →
        # token_expired (distinct code so SPA offers a re-login UX).
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(monkeypatch)

        now = int(time.time())
        token = _mint_rs256(
            pem, _default_claims(iat=now - 600, exp=now - 300)
        )
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "token_expired"}

    async def test_token_expired_within_leeway_succeeds(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        # exp = now-30, leeway = 60s → inside leeway → succeeds.
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(
            monkeypatch, existing_user=_FakeUser(uuid.UUID(TEST_SUB))
        )

        now = int(time.time())
        token = _mint_rs256(
            pem, _default_claims(iat=now - 120, exp=now - 30)
        )
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text

    async def test_wrong_audience_returns_401_invalid_token(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(monkeypatch)

        token = _mint_rs256(pem, _default_claims(aud="other-app"))
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}

    async def test_wrong_issuer_returns_401_invalid_token(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(monkeypatch)

        token = _mint_rs256(pem, _default_claims(iss="evil.com"))
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}

    async def test_nbf_in_future_returns_401_invalid_token(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        # nbf 1h in the future — outside any reasonable leeway.
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(monkeypatch)

        now = int(time.time())
        token = _mint_rs256(
            pem, _default_claims(nbf=now + 3600, iat=now, exp=now + 7200)
        )
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"error": "invalid_token"}


# ── 16-19, 24: Role claim / precedence ──────────────────────


@pytest.mark.unit
class TestRoles:
    async def test_empty_roles_returns_403_no_role_assigned(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(monkeypatch)

        token = _mint_rs256(pem, _default_claims(roles=[]))
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"error": "no_role_assigned"}

    async def test_unrecognized_roles_only_returns_403_no_role_assigned(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(monkeypatch)

        token = _mint_rs256(pem, _default_claims(roles=["weirdo"]))
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"error": "no_role_assigned"}

    async def test_role_precedence_super_admin_wins(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(
            monkeypatch,
            existing_user=_FakeUser(uuid.UUID(TEST_SUB), role="user"),
        )

        token = _mint_rs256(
            pem, _default_claims(roles=["user", "admin", "super_admin"])
        )
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "super_admin"

    async def test_role_precedence_admin_over_user(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(
            monkeypatch,
            existing_user=_FakeUser(uuid.UUID(TEST_SUB), role="user"),
        )

        token = _mint_rs256(pem, _default_claims(roles=["user", "admin"]))
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "admin"

    async def test_role_from_jwt_not_from_db(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        """Load-bearing: JWT roles win even when the local mirror says
        otherwise. If a future refactor accidentally starts reading
        ``user.role`` for authz, THIS test catches it — the DB row
        says ``admin`` while the JWT carries only ``user``.
        """
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(
            monkeypatch,
            existing_user=_FakeUser(uuid.UUID(TEST_SUB), role="admin"),
        )

        token = _mint_rs256(pem, _default_claims(roles=["user"]))
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "user"


# ── 20-21: Mirror auto-create ──────────────────────────────


@pytest.mark.unit
class TestMirrorAutocreate:
    async def test_missing_local_user_auto_mirrors(
        self, client, monkeypatch, rsa_keypair, test_jwk, caplog
    ):
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        created = _FakeUser(
            uuid.UUID(TEST_SUB), email=TEST_EMAIL, display_name="Alice"
        )
        captured = _patch_repo(
            monkeypatch, existing_user=None, upsert_result=created
        )

        token = _mint_rs256(pem, _default_claims())
        with caplog.at_level(logging.INFO, logger="app.core.auth"):
            resp = await client.get(
                "/me", headers={"Authorization": f"Bearer {token}"}
            )

        assert resp.status_code == 200, resp.text
        assert captured["upsert_called"] is True
        assert any(
            "mirror_autocreated" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]
        assert resp.json()["email"] == TEST_EMAIL

    async def test_auto_mirror_duplicate_email_returns_500(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(
            monkeypatch,
            existing_user=None,
            upsert_raises=DuplicateEmailInMirror(
                email=TEST_EMAIL,
                attempted_id=uuid.UUID(TEST_SUB),
            ),
        )

        token = _mint_rs256(pem, _default_claims())
        resp = await client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == {
            "error": "duplicate_email_in_mirror"
        }


# ── 22-23: require_roles ────────────────────────────────────


@pytest.mark.unit
class TestRequireRoles:
    async def test_require_roles_allows_match(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(
            monkeypatch,
            existing_user=_FakeUser(uuid.UUID(TEST_SUB), role="admin"),
        )

        token = _mint_rs256(pem, _default_claims(roles=["admin"]))
        resp = await client.get(
            "/admin", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "admin"

    async def test_require_roles_rejects_mismatch(
        self, client, monkeypatch, rsa_keypair, test_jwk
    ):
        _, pem = rsa_keypair
        _patch_jwks_calls(monkeypatch, [{"keys": [test_jwk]}])
        _patch_repo(
            monkeypatch,
            existing_user=_FakeUser(uuid.UUID(TEST_SUB), role="user"),
        )

        token = _mint_rs256(pem, _default_claims(roles=["user"]))
        resp = await client.get(
            "/admin", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"error": "forbidden"}
