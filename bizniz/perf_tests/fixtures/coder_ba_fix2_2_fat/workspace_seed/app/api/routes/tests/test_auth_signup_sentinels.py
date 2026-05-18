"""Dedicated sentinel tests for POST /api/v1/auth/signup (BA-fix2-1).

These lock the security-critical invariants the QE audit flagged on
BE-007-U3. They live in a separate file from ``test_auth_signup.py``
on purpose — the existing file is the behavior suite; this one is the
security firewall. A future refactor that "helpfully" simplifies
signup MUST keep these green or it has reopened a known vulnerability.

Covered sentinels:

1. Weak-password mapping — ``FusionAuthValidationError`` with
   ``fieldErrors['user.password']`` produces 400 + body
   ``{error:'weak_password', fields:{password:[...]}}`` and the mirror
   is NOT called.
2. FA-unavailable — ``FusionAuthUnavailable`` from ``register_user``
   produces 503 ``auth_service_unavailable`` and ``upsert_user_mirror``
   is NEVER called (no orphan partial state).
3. Password-never-logged — caplog at DEBUG across happy + weak-password
   + duplicate-registration + FA-down paths; assert the submitted
   password string is absent from every log record's message, every
   ``extra={...}`` attribute, and any exception repr.
4. Response omits password — deep-walk the 201 JSON; assert no key
   contains the substring ``password`` (case-insensitive) and no value
   anywhere in the tree equals the submitted password.
5. Validation 422 paths — missing email, missing password, malformed
   email each return 422 without calling FusionAuth.
"""
# Settings() requires the three FA env vars at import time. Seed safe
# defaults BEFORE importing anything that pulls in app.core.config.
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
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import auth_signup as _signup_mod
from app.db.session import get_db
from app.services.fusionauth_client import (
    FusionAuthUnavailable,
    FusionAuthValidationError,
)


# ── Fixtures + helpers ───────────────────────────────────


_FA_USER_ID = "11111111-2222-3333-4444-555555555555"
# A password that satisfies the FA policy (8+ chars, mixed case, digit,
# symbol) AND is a recognisable sentinel so we can grep logs for it.
_SENTINEL_PASSWORD = "Hunter2-S3cret!"
_PAYLOAD = {
    "email": "cook@example.com",
    "password": _SENTINEL_PASSWORD,
    "display_name": "Cook",
}


def _make_user_row(
    user_id: str = _FA_USER_ID,
    email: str = "cook@example.com",
    display_name: str | None = "Cook",
) -> MagicMock:
    """Stand-in for the User ORM row the route returns to the client."""
    row = MagicMock()
    row.id = UUID(user_id)
    row.email = email
    row.display_name = display_name
    row.role = "user"
    return row


def _async_bridge_session(session: MagicMock) -> MagicMock:
    """Attach async-friendly ``run_sync``/``commit``/``rollback``.

    The signup route awaits ``db.run_sync(...)`` + ``db.commit()`` on
    the mirror path; a plain MagicMock returns non-awaitable mocks
    from those methods. ``run_sync`` invokes its lambda against the
    same session so the patched ``upsert_user_mirror`` is exercised.
    """
    async def _run_sync(fn, *args, **kwargs):
        return fn(session, *args, **kwargs)

    session.run_sync = AsyncMock(side_effect=_run_sync)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _client(session: MagicMock | None = None) -> TestClient:
    """Spin up a bare FastAPI app with the signup router + a fake db."""
    sess = _async_bridge_session(session or MagicMock())
    app = FastAPI()
    app.include_router(_signup_mod.router, prefix="/api/v1")

    async def _override_get_db():
        yield sess

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _captured_text_blob(caplog: pytest.LogCaptureFixture) -> str:
    """Flatten every record (message + extras + repr(msg) + exc) to a string.

    The password-never-logged sentinel uses this as the haystack —
    every place a logger could write the password is concatenated so
    a single ``assert sentinel not in blob`` covers all leakage paths.
    """
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
        # repr(rec.msg) covers callers that pass a dict / non-string msg.
        parts.append(repr(rec.msg))
        if rec.exc_text:
            parts.append(rec.exc_text)
        # extras land directly on the record __dict__ — grab every key
        # that isn't a standard LogRecord field.
        for k, v in rec.__dict__.items():
            if k in standard:
                continue
            parts.append(f"{k}={v!r}")
    return "\n".join(parts)


# ── #1 Weak-password 400 mapping ─────────────────────────


class TestSignupWeakPasswordMapping:
    """FA ``fieldErrors['user.password']`` → 400 weak_password envelope.

    Sentinel for the audit finding ``[important] Weak-password
    mapping``. Locks the exact response shape the SPA renders against.
    """

    def test_weak_password_returns_400_envelope_and_no_mirror(self, monkeypatch):
        msgs = [
            {"code": "[tooShort]user.password"},
            {"code": "[lacksDigit]user.password"},
        ]
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=400,
                    body={"fieldErrors": {"user.password": msgs}},
                )
            ),
        )
        mirror_mock = MagicMock()
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", mirror_mock)

        resp = _client().post("/api/v1/auth/signup", json=_PAYLOAD)

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "weak_password"
        assert "fields" in detail
        assert "password" in detail["fields"]
        assert detail["fields"]["password"] == msgs
        # Mirror MUST NOT be touched when FA register failed.
        mirror_mock.assert_not_called()


# ── #2 FA-unavailable 503 + mirror NOT called ────────────


class TestSignupFaUnavailableSentinel:
    """``FusionAuthUnavailable`` from register → 503 + mirror not called.

    Sentinel for the audit finding ``[important] FA-unavailable
    mapping``. The point is the negative assertion — no orphan partial
    state in the local mirror when FA blew up.
    """

    def test_fa_unavailable_returns_503_and_mirror_not_called(self, monkeypatch):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthUnavailable(status_code=None, body=None)
            ),
        )
        mirror_mock = MagicMock()
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", mirror_mock)
        login_mock = AsyncMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "login", login_mock
        )

        resp = _client().post("/api/v1/auth/signup", json=_PAYLOAD)

        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}
        mirror_mock.assert_not_called()
        login_mock.assert_not_called()

    def test_fa_5xx_also_503_and_mirror_not_called(self, monkeypatch):
        # FA returning 5xx (vs transport-level failure) — same surface.
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthUnavailable(
                    status_code=503, body={"upstream": "down"}
                )
            ),
        )
        mirror_mock = MagicMock()
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", mirror_mock)

        resp = _client().post("/api/v1/auth/signup", json=_PAYLOAD)
        assert resp.status_code == 503
        mirror_mock.assert_not_called()


# ── #3 Password-never-logged sentinel ────────────────────


class TestSignupPasswordNeverLogged:
    """Submitted password MUST NOT appear in any log record on any branch.

    Sentinel for the audit finding ``[critical] Password-never-logged``.
    caplog is captured at DEBUG so even a future ``logger.debug(payload)``
    is caught. Each branch (happy, weak-password, duplicate-registration,
    FA-down) is exercised independently.
    """

    def _assert_password_absent(self, caplog: pytest.LogCaptureFixture) -> None:
        blob = _captured_text_blob(caplog)
        assert _SENTINEL_PASSWORD not in blob, (
            f"sentinel password leaked into logs:\n{blob}"
        )

    def test_happy_path_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {"id": _FA_USER_ID}}),
        )
        monkeypatch.setattr(
            _signup_mod,
            "upsert_user_mirror",
            MagicMock(return_value=_make_user_row()),
        )
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": "t"}),
        )
        with caplog.at_level(logging.DEBUG):
            resp = _client().post("/api/v1/auth/signup", json=_PAYLOAD)
        assert resp.status_code == 201, resp.text
        self._assert_password_absent(caplog)

    def test_weak_password_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=400,
                    body={
                        "fieldErrors": {
                            "user.password": [
                                {"code": "[tooShort]user.password"}
                            ]
                        }
                    },
                )
            ),
        )
        with caplog.at_level(logging.DEBUG):
            resp = _client().post("/api/v1/auth/signup", json=_PAYLOAD)
        assert resp.status_code == 400
        self._assert_password_absent(caplog)

    def test_duplicate_registration_branch_redacts_password(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=400,
                    body={
                        "fieldErrors": {
                            "[duplicate]registration": [
                                {"code": "[duplicate]registration"}
                            ]
                        }
                    },
                )
            ),
        )
        with caplog.at_level(logging.DEBUG):
            resp = _client().post("/api/v1/auth/signup", json=_PAYLOAD)
        assert resp.status_code == 500
        self._assert_password_absent(caplog)

    def test_fa_unavailable_branch_redacts_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthUnavailable(status_code=None, body=None)
            ),
        )
        with caplog.at_level(logging.DEBUG):
            resp = _client().post("/api/v1/auth/signup", json=_PAYLOAD)
        assert resp.status_code == 503
        self._assert_password_absent(caplog)


# ── #4 Response omits password (deep walk) ───────────────


class TestSignupResponseOmitsPassword:
    """The 201 body must never contain a password-shaped key or value.

    Sentinel for ``[important] Response-omits-password``. Strict deep
    walk: NO key whose name contains ``password`` (case-insensitive)
    appears anywhere in the tree, AND the submitted password value
    appears nowhere in the serialised body.
    """

    def _walk_assert_no_password_key(self, node, path: str = "$") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                # Substring match — defends against future fields like
                # ``password_hash``, ``passwordPolicy``, etc. that might
                # be added by a thoughtless ``.model_dump()`` of the
                # wrong type.
                assert "password" not in k.lower(), (
                    f"response body contains password-shaped key at "
                    f"{path}.{k!r}: full body fragment={node!r}"
                )
                self._walk_assert_no_password_key(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                self._walk_assert_no_password_key(item, f"{path}[{i}]")

    def _walk_assert_no_password_value(
        self, node, password: str, path: str = "$"
    ) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                self._walk_assert_no_password_value(
                    v, password, f"{path}.{k}"
                )
        elif isinstance(node, list):
            for i, item in enumerate(node):
                self._walk_assert_no_password_value(
                    item, password, f"{path}[{i}]"
                )
        elif isinstance(node, str):
            assert password not in node, (
                f"submitted password leaked into response body at "
                f"{path!r}: value={node!r}"
            )

    def test_201_response_has_no_password_key_or_value(self, monkeypatch):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {"id": _FA_USER_ID}}),
        )
        monkeypatch.setattr(
            _signup_mod,
            "upsert_user_mirror",
            MagicMock(return_value=_make_user_row()),
        )
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": "t"}),
        )

        resp = _client().post("/api/v1/auth/signup", json=_PAYLOAD)
        assert resp.status_code == 201, resp.text

        body = resp.json()
        self._walk_assert_no_password_key(body)
        self._walk_assert_no_password_value(body, _SENTINEL_PASSWORD)
        # Belt-and-braces: the raw response text must also not contain
        # the sentinel (catches a future leak through a renamed key
        # whose value happens to be the password).
        assert _SENTINEL_PASSWORD not in resp.text


# ── #5 Validation 422 path ───────────────────────────────


class TestSignupValidationReturns422:
    """Pydantic-level rejection MUST be 422 and MUST NOT call FA.

    Sentinel for ``[important] Validation 422 path``. Locks both the
    status code AND the no-side-effect contract (no upstream call when
    the request body never satisfied the schema).
    """

    def test_missing_email_returns_422_without_calling_fa(self, monkeypatch):
        register_mock = AsyncMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", register_mock
        )
        resp = _client().post(
            "/api/v1/auth/signup",
            json={"password": _SENTINEL_PASSWORD, "display_name": "Cook"},
        )
        assert resp.status_code == 422
        register_mock.assert_not_called()

    def test_missing_password_returns_422_without_calling_fa(self, monkeypatch):
        register_mock = AsyncMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", register_mock
        )
        resp = _client().post(
            "/api/v1/auth/signup",
            json={"email": "cook@example.com", "display_name": "Cook"},
        )
        assert resp.status_code == 422
        register_mock.assert_not_called()

    def test_malformed_email_returns_422_without_calling_fa(self, monkeypatch):
        register_mock = AsyncMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", register_mock
        )
        resp = _client().post(
            "/api/v1/auth/signup",
            json={
                "email": "not-an-email",
                "password": _SENTINEL_PASSWORD,
            },
        )
        assert resp.status_code == 422
        register_mock.assert_not_called()
