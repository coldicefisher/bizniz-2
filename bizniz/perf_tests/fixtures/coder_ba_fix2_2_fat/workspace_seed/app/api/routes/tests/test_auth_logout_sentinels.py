"""Dedicated sentinel tests for POST /api/v1/auth/logout (BA-fix2-1).

These lock the security-critical invariants the QE audit flagged on
BE-009-U2. They sit beside ``test_auth_logout.py`` (the behavior suite)
as a focused security firewall — a future "helpful" refactor MUST
keep these green or it has reopened a known vulnerability.

Logout is the simplest auth route in this milestone: stateless,
idempotent, and the spec demands that EVERY input shape — including
malformed bearer tokens, expired JWTs, JWKS outages, and unexpected
exceptions from the audit hook — returns 204 with an EMPTY body.
The whole point of this file is the "always-204" matrix and the
"empty body" protocol assertion.

Covered failure modes (all MUST return 204 with empty body):

1. Missing Authorization header — anonymous logout.
2. ``Bearer `` with no token — malformed but technically Bearer.
3. ``Bearer garbage`` — non-JWT-shaped token.
4. Expired token — mocked validator raises ``HTTPException(401,
   token_expired)``.
5. JWKS unavailable — mocked validator raises ``HTTPException(503,
   auth_service_unavailable)``. The load-bearing 503-swallow sentinel.
6. Unexpected exception — mocked validator raises a non-HTTP
   exception (``RuntimeError``). Locks the "bare except is
   intentional" contract.
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

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes.auth_logout import router
from app.core import auth as core_auth


def _client() -> TestClient:
    """Bare FastAPI app with only the logout router mounted.

    No DB, no get_current_user — logout deliberately bypasses the
    standard auth dependency so missing/garbage tokens cannot leak
    401s out of the route.
    """
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _patch_validate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verify_exc: BaseException | None = None,
    claims: dict | None = None,
) -> None:
    """Install fake JWT helpers on ``app.core.auth``.

    Either ``verify_exc`` to make the signature/claims step raise, or
    ``claims`` to make it return successfully. Header decode always
    succeeds with a plausible value — we want the failure to surface
    from the signature/claims step where the spec wants it.
    """

    def _fake_header(_token: str) -> dict:
        return {"alg": "RS256", "kid": "test-kid"}

    async def _fake_verify(_token: str, _header: dict) -> dict:
        if verify_exc is not None:
            raise verify_exc
        return claims if claims is not None else {}

    monkeypatch.setattr(core_auth, "_decode_unverified_header", _fake_header)
    monkeypatch.setattr(
        core_auth, "_verify_jwt_signature_and_claims", _fake_verify
    )


def _assert_204_empty(resp) -> None:
    """The protocol assertion: 204 status AND empty bytes body."""
    assert resp.status_code == 204, (
        f"expected 204, got {resp.status_code}: {resp.text!r}"
    )
    # 204 with a JSON payload is a protocol violation. response.content
    # MUST be exactly empty bytes — not b'null', not b'{}'.
    assert resp.content == b"", (
        f"204 response body MUST be empty bytes; got {resp.content!r}"
    )


# ── The always-204 matrix (6 distinct failure modes) ─────


class TestLogoutAlways204Matrix:
    """The load-bearing "logout is idempotent" sentinel.

    Each test exercises a distinct failure mode that a future careless
    refactor could turn into a 4xx/5xx. The matrix together asserts
    NO input shape escapes the bare ``except Exception: pass`` wrap
    around the audit path.
    """

    def test_missing_authorization_header_returns_204_empty(self):
        resp = _client().post("/auth/logout")
        _assert_204_empty(resp)

    def test_bearer_empty_token_returns_204_empty(self):
        # "Bearer " with a trailing space and no actual token.
        resp = _client().post(
            "/auth/logout", headers={"Authorization": "Bearer "}
        )
        _assert_204_empty(resp)

    def test_bearer_garbage_token_returns_204_empty(self, monkeypatch):
        # "Bearer notajwt" — header decode will raise, audit path
        # swallows it. We don't patch the validators here because the
        # real ``_decode_unverified_header`` will raise on garbage,
        # which is exactly the path we want to exercise.
        resp = _client().post(
            "/auth/logout", headers={"Authorization": "Bearer notajwt"}
        )
        _assert_204_empty(resp)

    def test_expired_token_via_mocked_validator_returns_204_empty(
        self, monkeypatch
    ):
        _patch_validate(
            monkeypatch,
            verify_exc=HTTPException(
                status_code=401, detail={"error": "token_expired"}
            ),
        )
        resp = _client().post(
            "/auth/logout",
            headers={"Authorization": "Bearer aaa.bbb.ccc"},
        )
        _assert_204_empty(resp)

    def test_jwks_unavailable_via_mocked_dependency_returns_204_empty(
        self, monkeypatch
    ):
        # Load-bearing — without this someone could "helpfully" add
        # ``except HTTPException as e: raise e`` and silently break
        # the "logout always succeeds" contract during a FA outage.
        _patch_validate(
            monkeypatch,
            verify_exc=HTTPException(
                status_code=503,
                detail={"error": "auth_service_unavailable"},
            ),
        )
        resp = _client().post(
            "/auth/logout",
            headers={"Authorization": "Bearer aaa.bbb.ccc"},
        )
        _assert_204_empty(resp)

    def test_unexpected_exception_in_audit_helper_returns_204_empty(
        self, monkeypatch
    ):
        # The "bare except is intentional" sentinel. If a future
        # refactor narrows the except clause to specific exception
        # types, this test fires for the non-listed type.
        _patch_validate(
            monkeypatch,
            verify_exc=RuntimeError("audit helper exploded"),
        )
        resp = _client().post(
            "/auth/logout",
            headers={"Authorization": "Bearer aaa.bbb.ccc"},
        )
        _assert_204_empty(resp)
