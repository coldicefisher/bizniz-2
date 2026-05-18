"""Sentinel for BA-fix2-3 login DuplicateEmailInMirror error code.

CodeReviewer flagged that the login route's mirror auto-create branch
returned 500 with ``{"error": "user_mirror_failed"}`` while the parallel
``/api/me`` path in :mod:`app.core.auth` mapped the same
:class:`DuplicateEmailInMirror` to 500 ``duplicate_email_in_mirror`` —
the two endpoints should agree so SPA error-handling stays uniform.

This test pins the post-repair contract: when ``upsert_user_mirror``
raises :class:`DuplicateEmailInMirror` on the self-heal path after a
successful FA login + JWT validation, the response is 500 AND the body's
``error`` field is the canonical ``duplicate_email_in_mirror`` code (NOT
the old ``user_mirror_failed``).
"""
# Settings() at app.core.config import time requires FUSIONAUTH_* env
# vars; ``setdefault`` so any real env still wins.
import os as _os

_os.environ.setdefault(
    "FUSIONAUTH_TENANT_ID", "d4465dd9-12e7-4715-bc4e-690874974b6b"
)
_os.environ.setdefault(
    "FUSIONAUTH_APPLICATION_ID", "85a03867-dccf-4882-adde-1a79aeec50df"
)
_os.environ.setdefault("FUSIONAUTH_API_KEY", "test-api-key-xyz")

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import auth_login as _login_mod
from app.db.session import get_db
from app.repositories.user_repository import DuplicateEmailInMirror


_FA_USER_ID = "11111111-2222-3333-4444-555555555555"
_FA_TOKEN = "header.payload.signature"
_VALID_PAYLOAD = {"email": "user@example.com", "password": "password"}


def _async_bridge_session(session: MagicMock) -> MagicMock:
    """Attach awaitable ``run_sync`` / ``commit`` / ``rollback`` to ``session``.

    The login route bridges the sync ``upsert_user_mirror`` onto an
    AsyncSession via ``await db.run_sync(...)`` and follows up with
    ``await db.commit()`` or ``await db.rollback()``. Plain MagicMocks
    return non-awaitable MagicMocks from these calls, so we replace them
    with AsyncMocks. ``run_sync`` actually invokes its callable so the
    route's lambda drives the patched ``upsert_user_mirror`` MagicMock.
    """
    async def _run_sync(fn, *args, **kwargs):
        return fn(session, *args, **kwargs)

    session.run_sync = AsyncMock(side_effect=_run_sync)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _build_client(session: MagicMock) -> TestClient:
    """Spin up a FastAPI app with just the login router + overridden get_db."""
    _async_bridge_session(session)
    app = FastAPI()
    app.include_router(_login_mod.router, prefix="/api/v1")

    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _install_jwt_validation(monkeypatch, *, claims: dict) -> None:
    """Patch the BE-006 JWT helpers to succeed with the supplied claims."""
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


class TestLoginDuplicateEmailInMirrorErrorCode:
    """The login route MUST return 500 ``duplicate_email_in_mirror``."""

    def test_dup_mirror_returns_500_with_duplicate_email_in_mirror_code(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
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

        client = _build_client(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 500, resp.text
        body = resp.json()
        # Canonical post-repair error code — matches the /api/me handler
        # in app.core.auth so SPA error handling is uniform.
        assert body["detail"] == {"error": "duplicate_email_in_mirror"}

    def test_dup_mirror_no_longer_uses_old_user_mirror_failed_code(
        self, monkeypatch
    ):
        # Companion assertion: the OLD code MUST NOT come back. If a
        # future refactor accidentally re-introduces "user_mirror_failed",
        # this test trips immediately at PR time.
        monkeypatch.setattr(
            _login_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": _FA_TOKEN}),
        )
        _install_jwt_validation(
            monkeypatch,
            claims={
                "sub": _FA_USER_ID,
                "email": "user@example.com",
                "roles": ["user"],
            },
        )
        monkeypatch.setattr(
            _login_mod, "get_user_by_id", AsyncMock(return_value=None)
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

        client = _build_client(MagicMock())
        resp = client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.json()["detail"] != {"error": "user_mirror_failed"}
        assert "user_mirror_failed" not in resp.text
