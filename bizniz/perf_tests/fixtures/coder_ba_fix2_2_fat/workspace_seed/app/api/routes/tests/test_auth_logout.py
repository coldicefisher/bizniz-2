"""Unit tests for /api/auth/logout — BE-009-U2.

Covers the 12 success-criteria cases from the issue: the best-effort
204 contract, the audit-log shape (event/user_id/ts UTC), and the
load-bearing "no error mode escapes" sentinels (#7 swallow 503, #8
swallow unexpected non-HTTP exceptions). Together these lock the
"logout is idempotent and always returns 204" contract from the
auth spec.

We mount only the logout router on a bare FastAPI app — no DB, no
auto-discovery — and use FastAPI's sync TestClient so cases stay
fast and hermetic. Validation is exercised via monkeypatching the
two helpers the route calls into (``_decode_unverified_header`` and
``_verify_jwt_signature_and_claims``); the real JWKS / FusionAuth
machinery is never hit.
"""
# Settings() requires the three required FusionAuth env vars at
# import time. Seed safe defaults BEFORE importing anything that
# pulls in ``app.core.config``.
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
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes import auth_logout
from app.api.routes.auth_logout import router
from app.core import auth as core_auth


# ── Test app / client ────────────────────────────────────


def _make_app() -> FastAPI:
    """Mount only the logout router on a bare app (no DB, no lifespan)."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client() -> TestClient:
    """Sync TestClient against an app mounting only the logout router."""
    return TestClient(_make_app())


def _patch_validate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claims: dict | None = None,
    verify_exc: BaseException | None = None,
    header_exc: BaseException | None = None,
) -> None:
    """Replace the two JWT helpers the route calls into.

    Either supply ``claims`` for the happy path, ``verify_exc`` to make
    the signature/claims step blow up, or ``header_exc`` to make the
    header-decode step blow up. Lets each test express its intent in
    one line.
    """

    def _fake_header(_token: str) -> dict:
        if header_exc is not None:
            raise header_exc
        return {"alg": "RS256", "kid": "test-kid"}

    async def _fake_verify(_token: str, _header: dict) -> dict:
        if verify_exc is not None:
            raise verify_exc
        return claims if claims is not None else {}

    monkeypatch.setattr(core_auth, "_decode_unverified_header", _fake_header)
    monkeypatch.setattr(
        core_auth, "_verify_jwt_signature_and_claims", _fake_verify
    )


# ── The 12 success-criteria cases ────────────────────────


def test_logout_no_auth_header_returns_204(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Case 1: no Authorization header → 204, empty body, no audit log."""
    with caplog.at_level(logging.INFO, logger=auth_logout.logger.name):
        resp = client.post("/auth/logout")
    assert resp.status_code == 204
    assert resp.content == b""
    # Defensive: no audit log emitted for anon logout.
    assert [r for r in caplog.records if r.message == "logout"] == []


def test_logout_valid_jwt_returns_204_and_audits(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Case 2: valid JWT → 204, INFO 'logout' record with user_id + ts."""
    sub = str(uuid.uuid4())
    _patch_validate(
        monkeypatch,
        claims={"sub": sub, "email": "a@b.c", "roles": ["user"]},
    )

    with caplog.at_level(logging.INFO, logger=auth_logout.logger.name):
        resp = client.post(
            "/auth/logout",
            headers={"Authorization": "Bearer aaa.bbb.ccc"},
        )
    assert resp.status_code == 204

    records = [r for r in caplog.records if r.message == "logout"]
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.INFO
    assert getattr(rec, "event") == "logout"
    assert getattr(rec, "user_id") == sub
    ts = getattr(rec, "ts")
    assert isinstance(ts, str)
    # Must parse as ISO-8601 via datetime.fromisoformat.
    datetime.fromisoformat(ts)


def test_logout_malformed_header_returns_204(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Case 3: 'NotBearer xyz' → 204, no audit log."""
    with caplog.at_level(logging.INFO, logger=auth_logout.logger.name):
        resp = client.post(
            "/auth/logout", headers={"Authorization": "NotBearer xyz"}
        )
    assert resp.status_code == 204
    assert [r for r in caplog.records if r.message == "logout"] == []


def test_logout_empty_bearer_returns_204(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Case 4: 'Bearer ' (no token) → 204, no audit log."""
    with caplog.at_level(logging.INFO, logger=auth_logout.logger.name):
        resp = client.post(
            "/auth/logout", headers={"Authorization": "Bearer "}
        )
    assert resp.status_code == 204
    assert [r for r in caplog.records if r.message == "logout"] == []


def test_logout_expired_token_returns_204(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Case 5: validate raises 401 token_expired → 204, swallowed, no log."""
    _patch_validate(
        monkeypatch,
        verify_exc=HTTPException(
            status_code=401, detail={"error": "token_expired"}
        ),
    )

    with caplog.at_level(logging.INFO, logger=auth_logout.logger.name):
        # Must not propagate the HTTPException — should be swallowed.
        resp = client.post(
            "/auth/logout",
            headers={"Authorization": "Bearer aaa.bbb.ccc"},
        )
    assert resp.status_code == 204
    assert [r for r in caplog.records if r.message == "logout"] == []


def test_logout_invalid_signature_returns_204(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 6: validate raises 401 invalid_token → 204."""
    _patch_validate(
        monkeypatch,
        verify_exc=HTTPException(
            status_code=401, detail={"error": "invalid_token"}
        ),
    )

    resp = client.post(
        "/auth/logout",
        headers={"Authorization": "Bearer aaa.bbb.ccc"},
    )
    assert resp.status_code == 204


def test_logout_jwks_unavailable_returns_204(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 7 (load-bearing): even 503 auth_service_unavailable → 204.

    This is THE sentinel that prevents a future refactor from
    'helpfully' adding ``except HTTPException as e: raise e`` and
    silently breaking the 'logout must always succeed' contract.
    """
    _patch_validate(
        monkeypatch,
        verify_exc=HTTPException(
            status_code=503, detail={"error": "auth_service_unavailable"}
        ),
    )

    resp = client.post(
        "/auth/logout",
        headers={"Authorization": "Bearer aaa.bbb.ccc"},
    )
    assert resp.status_code == 204


def test_logout_validate_raises_unexpected_exception_returns_204(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 8: non-HTTP exception (ValueError) → 204.

    Locks the "bare except is intentional" contract: narrowing the
    audit-path try/except to specific types would re-introduce the
    5xx leak the route exists to prevent.
    """
    _patch_validate(
        monkeypatch, verify_exc=ValueError("something weird")
    )

    resp = client.post(
        "/auth/logout",
        headers={"Authorization": "Bearer aaa.bbb.ccc"},
    )
    assert resp.status_code == 204


def test_logout_get_returns_405(client: TestClient) -> None:
    """Case 9: GET on logout path → 405 (FastAPI default for POST-only)."""
    resp = client.get("/auth/logout")
    assert resp.status_code == 405


def test_logout_response_body_is_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 10: happy-path response body MUST be empty (204 protocol)."""
    _patch_validate(
        monkeypatch,
        claims={"sub": str(uuid.uuid4()), "roles": ["user"]},
    )
    resp = client.post(
        "/auth/logout",
        headers={"Authorization": "Bearer aaa.bbb.ccc"},
    )
    assert resp.status_code == 204
    assert resp.content == b""


def test_logout_valid_jwt_no_sub_no_audit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Case 11: validated claims missing 'sub' → 204, no audit log.

    Defensive: don't log ``user_id=None``. The audit trail is only
    useful when we can attribute the event to a real user.
    """
    _patch_validate(monkeypatch, claims={})  # no 'sub' key

    with caplog.at_level(logging.INFO, logger=auth_logout.logger.name):
        resp = client.post(
            "/auth/logout",
            headers={"Authorization": "Bearer aaa.bbb.ccc"},
        )
    assert resp.status_code == 204
    assert [r for r in caplog.records if r.message == "logout"] == []


def test_logout_ts_is_utc(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Case 12: audit ts MUST be UTC (locks the spec'd timezone)."""
    _patch_validate(
        monkeypatch,
        claims={"sub": str(uuid.uuid4()), "roles": ["user"]},
    )

    with caplog.at_level(logging.INFO, logger=auth_logout.logger.name):
        resp = client.post(
            "/auth/logout",
            headers={"Authorization": "Bearer aaa.bbb.ccc"},
        )
    assert resp.status_code == 204

    records = [r for r in caplog.records if r.message == "logout"]
    assert len(records) == 1
    ts = getattr(records[0], "ts")
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    # UTC iff its offset is zero (timezone.utc OR a +00:00 fixed offset).
    assert parsed.utcoffset() == timedelta(0)
