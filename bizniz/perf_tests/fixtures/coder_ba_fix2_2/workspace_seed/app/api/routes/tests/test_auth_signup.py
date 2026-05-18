"""Unit tests for the auth_signup module scaffold (BE-007-U1).

Covers the module-level invariants set by the scaffold issue:

* ``router`` is a properly-configured ``APIRouter`` whose prefix and
  tags match the skeleton's auto-mount contract (auto-mount under
  ``settings.api_v1_prefix`` adds ``/api/v1`` — the router declares
  only ``/auth`` to avoid double-prefixing).
* ``_translate_fa_signup_error`` maps each documented FusionAuth
  ``fieldErrors`` shape to the right HTTPException without exercising
  any I/O.

The full route handler tests land in BE-007-U3.
"""
# Settings() requires FUSIONAUTH_TENANT_ID even though tenant_id isn't
# referenced by this scaffold. The dev container env is missing it, so
# we fill in safe defaults here BEFORE importing app.core.config (which
# instantiates the module-level singleton at import time). setdefault
# means a real env var still wins if one is present.
import os as _os

_os.environ.setdefault(
    "FUSIONAUTH_TENANT_ID", "d4465dd9-12e7-4715-bc4e-690874974b6b"
)
_os.environ.setdefault(
    "FUSIONAUTH_APPLICATION_ID", "85a03867-dccf-4882-adde-1a79aeec50df"
)
_os.environ.setdefault("FUSIONAUTH_API_KEY", "test-api-key-xyz")

import logging

import pytest
from fastapi import APIRouter, HTTPException

from app.api.routes import auth_signup
from app.api.routes.auth_signup import (
    _translate_fa_signup_error,
    router,
)
from app.services.fusionauth_client import FusionAuthValidationError


# ── Router scaffold ───────────────────────────────────────


class TestRouterScaffold:
    """The router declaration is the skeleton's only public contract."""

    def test_router_is_apirouter(self):
        assert isinstance(router, APIRouter)

    def test_router_prefix_is_auth_only(self):
        # Skeleton auto-mount adds /api/v1 — declaring /api/auth here
        # would double-prefix to /api/v1/api/auth. Match the existing
        # app/api/routes/auth.py convention: prefix='/auth'.
        assert router.prefix == "/auth"

    def test_router_tags_include_auth(self):
        assert router.tags == ["auth"]


# ── Imports the route handler will need ──────────────────


class TestModuleImports:
    """U2 needs these symbols importable at module load time."""

    def test_signup_request_imported(self):
        from app.schemas.auth import SignupRequest as _expected
        assert auth_signup.SignupRequest is _expected

    def test_auth_response_imported(self):
        from app.schemas.auth import AuthResponse as _expected
        assert auth_signup.AuthResponse is _expected

    def test_user_out_imported(self):
        from app.schemas.auth import UserOut as _expected
        assert auth_signup.UserOut is _expected

    def test_error_response_imported(self):
        from app.schemas.auth import ErrorResponse as _expected
        assert auth_signup.ErrorResponse is _expected

    def test_fusionauth_client_module_imported(self):
        from app.services import fusionauth_client as _fa
        assert auth_signup.fusionauth_client is _fa

    def test_fa_exceptions_imported(self):
        from app.services.fusionauth_client import (
            FusionAuthUnavailable as _u,
            FusionAuthValidationError as _v,
        )
        assert auth_signup.FusionAuthUnavailable is _u
        assert auth_signup.FusionAuthValidationError is _v

    def test_repository_symbols_imported(self):
        from app.repositories.user_repository import (
            upsert_user_mirror as _upsert,
            DuplicateEmailInMirror as _dup,
        )
        assert auth_signup.upsert_user_mirror is _upsert
        assert auth_signup.DuplicateEmailInMirror is _dup

    def test_get_db_imported(self):
        from app.db.session import get_db as _get_db
        assert auth_signup.get_db is _get_db

    def test_logger_named_for_module(self):
        assert isinstance(auth_signup.logger, logging.Logger)
        assert auth_signup.logger.name == "app.api.routes.auth_signup"


# ── _translate_fa_signup_error ───────────────────────────


def _validation_error_with(body) -> FusionAuthValidationError:
    return FusionAuthValidationError(status_code=400, body=body)


class TestTranslateFaSignupError:
    """Each documented FA fieldErrors shape maps to the right HTTPException."""

    def test_weak_password_returns_400_with_password_fields(self):
        msgs = [
            {"code": "[tooShort]user.password"},
            {"code": "[lacksDigit]user.password"},
        ]
        exc = _validation_error_with(
            {"fieldErrors": {"user.password": msgs}}
        )
        http_exc = _translate_fa_signup_error(exc)
        assert isinstance(http_exc, HTTPException)
        assert http_exc.status_code == 400
        assert http_exc.detail == {
            "error": "weak_password",
            "fields": {"password": msgs},
        }

    def test_weak_password_aggregates_subkeys(self):
        # FA can put multiple keys under user.password — e.g.
        # ``user.password`` plus ``user.password.minLength``. Both
        # should be surfaced together.
        exc = _validation_error_with({
            "fieldErrors": {
                "user.password": [{"code": "a"}],
                "user.password.minLength": [{"code": "b"}],
            }
        })
        http_exc = _translate_fa_signup_error(exc)
        assert http_exc.status_code == 400
        assert http_exc.detail["error"] == "weak_password"
        password_messages = http_exc.detail["fields"]["password"]
        assert {"code": "a"} in password_messages
        assert {"code": "b"} in password_messages

    def test_duplicate_email_returns_409(self):
        exc = _validation_error_with({
            "fieldErrors": {
                "[duplicate]user.email": [{"code": "[duplicate]user.email"}]
            }
        })
        http_exc = _translate_fa_signup_error(exc)
        assert http_exc.status_code == 409
        assert http_exc.detail == {"error": "email_already_registered"}

    def test_duplicate_registration_returns_500_and_logs_error(self, caplog):
        exc = _validation_error_with({
            "fieldErrors": {
                "[duplicate]registration": [{"code": "[duplicate]registration"}]
            }
        })
        with caplog.at_level(logging.ERROR, logger="app.api.routes.auth_signup"):
            http_exc = _translate_fa_signup_error(exc)
        assert http_exc.status_code == 500
        assert http_exc.detail == {"error": "auth_config_error"}
        assert any(
            "fa_config_error_duplicate_registration" in rec.getMessage()
            for rec in caplog.records
        )

    def test_unmapped_validation_returns_400_and_logs_warning(self, caplog):
        exc = _validation_error_with({
            "fieldErrors": {"user.email": [{"code": "[invalid]user.email"}]}
        })
        with caplog.at_level(logging.WARNING, logger="app.api.routes.auth_signup"):
            http_exc = _translate_fa_signup_error(exc)
        assert http_exc.status_code == 400
        assert http_exc.detail == {"error": "validation_error", "fields": {}}
        assert any(
            "fa_signup_validation_error_unmapped" in rec.getMessage()
            for rec in caplog.records
        )

    def test_empty_body_returns_unmapped_400(self):
        exc = _validation_error_with({})
        http_exc = _translate_fa_signup_error(exc)
        assert http_exc.status_code == 400
        assert http_exc.detail == {"error": "validation_error", "fields": {}}

    def test_non_dict_body_returns_unmapped_400(self):
        # _safe_body returns ``str`` for non-JSON FA error bodies; the
        # translator must not crash on that path.
        exc = _validation_error_with("internal server error")
        http_exc = _translate_fa_signup_error(exc)
        assert http_exc.status_code == 400
        assert http_exc.detail == {"error": "validation_error", "fields": {}}

    def test_password_takes_precedence_over_duplicate_email(self):
        # FA reports multiple field errors at once when several
        # constraints fail. Password-weakness is reported first because
        # FE wants to render those messages even if the email also
        # collides (user fixing both at once).
        exc = _validation_error_with({
            "fieldErrors": {
                "user.password": [{"code": "[tooShort]user.password"}],
                "[duplicate]user.email": [{"code": "[duplicate]user.email"}],
            }
        })
        http_exc = _translate_fa_signup_error(exc)
        assert http_exc.status_code == 400
        assert http_exc.detail["error"] == "weak_password"

    def test_redact_helper_replaces_password_keys(self):
        from app.api.routes.auth_signup import _redact_signup_body
        redacted = _redact_signup_body({
            "user": {"email": "x@y.com", "password": "secret"},
            "registration": {"roles": ["user"]},
        })
        assert redacted["user"]["password"] == "***"
        assert redacted["user"]["email"] == "x@y.com"
        assert redacted["registration"]["roles"] == ["user"]

    def test_returns_httpexception_not_raises(self):
        # Pure translation — must RETURN, never raise. Route layer
        # chooses when to raise so it can add ``raise from exc`` for
        # traceback preservation.
        exc = _validation_error_with({})
        result = _translate_fa_signup_error(exc)
        assert isinstance(result, HTTPException)


# ── POST /signup route handler (BE-007-U2) ───────────────


from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import auth_signup as _signup_mod
from app.db.session import get_db
from app.repositories.user_repository import DuplicateEmailInMirror
from app.services.fusionauth_client import FusionAuthUnavailable


_FA_USER_ID = "11111111-2222-3333-4444-555555555555"
_VALID_PAYLOAD = {
    "email": "cook@example.com",
    "password": "Password123!",
    "display_name": "Cook",
}


def _make_user_row(
    user_id: str = _FA_USER_ID,
    email: str = "cook@example.com",
    display_name: str | None = "Cook",
) -> MagicMock:
    """Build a stand-in for the User ORM row that the route returns."""
    row = MagicMock()
    row.id = UUID(user_id)
    row.email = email
    row.display_name = display_name
    row.role = "user"
    return row


def _async_bridge_session(session: MagicMock) -> MagicMock:
    """Attach async-friendly ``run_sync`` / ``commit`` / ``rollback`` to ``session``.

    Post-BA-fix1-1, the signup route uses ``await db.run_sync(...)`` and
    ``await db.commit()``. A plain ``MagicMock`` returns non-awaitable
    MagicMocks from method calls, so we replace those three methods
    with :class:`AsyncMock` instances. ``run_sync`` additionally invokes
    its callable so the route's lambda actually drives the patched
    ``upsert_user_mirror`` MagicMock attached to the module.
    """
    async def _run_sync(fn, *args, **kwargs):
        return fn(session, *args, **kwargs)

    session.run_sync = AsyncMock(side_effect=_run_sync)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _build_client_with_session(session: MagicMock) -> TestClient:
    """Spin up a FastAPI app with just the signup router and overridden get_db."""
    _async_bridge_session(session)
    app = FastAPI()
    app.include_router(_signup_mod.router, prefix="/api/v1")

    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


class TestSignupRouteHappyPath:
    """The 201 happy path: FA register → mirror → FA login → JWT."""

    def test_happy_path_returns_201_with_token_and_user(self, monkeypatch):
        session = MagicMock()
        user_row = _make_user_row()

        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {"id": _FA_USER_ID}}),
        )
        monkeypatch.setattr(
            _signup_mod,
            "upsert_user_mirror",
            MagicMock(return_value=user_row),
        )
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": "jwt.token.value"}),
        )

        client = _build_client_with_session(session)
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["token"] == "jwt.token.value"
        assert body["user"]["id"] == _FA_USER_ID
        assert body["user"]["email"] == "cook@example.com"
        assert body["user"]["display_name"] == "Cook"
        # Role is HARDCODED — never trust the payload (defense in depth).
        assert body["user"]["role"] == "user"
        session.commit.assert_called_once()

    def test_register_called_with_hardcoded_user_role(self, monkeypatch):
        # Even if a future SignupRequest gained a role field, this route
        # must ignore it. The hardcode is the defense-in-depth point.
        register_mock = AsyncMock(return_value={"user": {"id": _FA_USER_ID}})
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", register_mock
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

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)
        assert resp.status_code == 201

        register_mock.assert_awaited_once()
        kwargs = register_mock.await_args.kwargs
        assert kwargs["roles"] == ["user"]


class TestSignupRouteFaErrors:
    """FA-side failures map to the right HTTP codes."""

    def test_fa_unavailable_on_register_returns_503(self, monkeypatch):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthUnavailable(status_code=None, body=None)
            ),
        )
        # Mirror + login must not be called if FA register failed.
        mirror_mock = MagicMock()
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", mirror_mock)
        login_mock = AsyncMock()
        monkeypatch.setattr(_signup_mod.fusionauth_client, "login", login_mock)

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}
        mirror_mock.assert_not_called()
        login_mock.assert_not_called()

    def test_fa_duplicate_email_returns_409(self, monkeypatch):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=400,
                    body={
                        "fieldErrors": {
                            "[duplicate]user.email": [
                                {"code": "[duplicate]user.email"}
                            ]
                        }
                    },
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 409
        assert resp.json()["detail"] == {"error": "email_already_registered"}

    def test_fa_weak_password_returns_400(self, monkeypatch):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=400,
                    body={
                        "fieldErrors": {
                            "user.password": [{"code": "[tooShort]user.password"}]
                        }
                    },
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "weak_password"


class TestSignupRouteIdParsing:
    """Missing / invalid FA user id surfaces as 500 auth_config_error."""

    def test_missing_fa_user_id_returns_500_config_error(self, monkeypatch):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {}}),  # no id
        )
        mirror_mock = MagicMock()
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", mirror_mock)

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 500
        assert resp.json()["detail"] == {"error": "auth_config_error"}
        mirror_mock.assert_not_called()

    def test_non_uuid_fa_user_id_returns_500_config_error(self, monkeypatch):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {"id": "not-a-uuid"}}),
        )
        mirror_mock = MagicMock()
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", mirror_mock)

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 500
        assert resp.json()["detail"] == {"error": "auth_config_error"}
        mirror_mock.assert_not_called()


class TestSignupRouteMirrorErrors:
    """Local mirror failures surface as 500 user_mirror_failed."""

    def test_duplicate_email_in_mirror_returns_409_email_already_registered(
        self, monkeypatch
    ):
        # Post-BA-fix1-1: a ``DuplicateEmailInMirror`` from the mirror
        # upsert is now a 409 ``email_already_registered``, matching the
        # local_user_mirror error contract. Previously this was 500
        # ``user_mirror_failed`` — that surface hid a user-resolvable
        # collision behind a generic internal-error code.
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {"id": _FA_USER_ID}}),
        )
        monkeypatch.setattr(
            _signup_mod,
            "upsert_user_mirror",
            MagicMock(
                side_effect=DuplicateEmailInMirror(
                    email="cook@example.com",
                    attempted_id=UUID(_FA_USER_ID),
                )
            ),
        )
        login_mock = AsyncMock()
        monkeypatch.setattr(_signup_mod.fusionauth_client, "login", login_mock)

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 409
        assert resp.json()["detail"] == {"error": "email_already_registered"}
        login_mock.assert_not_called()

    def test_generic_mirror_failure_rolls_back_and_returns_500(self, monkeypatch):
        session = MagicMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {"id": _FA_USER_ID}}),
        )
        monkeypatch.setattr(
            _signup_mod,
            "upsert_user_mirror",
            MagicMock(side_effect=RuntimeError("db went sideways")),
        )

        client = _build_client_with_session(session)
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 500
        assert resp.json()["detail"] == {"error": "user_mirror_failed"}
        session.rollback.assert_called_once()


class TestSignupRoutePostLoginErrors:
    """Login-step failures after a successful register + mirror."""

    def test_fa_login_unavailable_post_signup_returns_503(self, monkeypatch):
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
            AsyncMock(
                side_effect=FusionAuthUnavailable(status_code=None, body=None)
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}

    def test_fa_login_validation_post_signup_returns_500_config(self, monkeypatch):
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
            AsyncMock(
                side_effect=FusionAuthValidationError(
                    status_code=404, body={"fieldErrors": {}}
                )
            ),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 500
        assert resp.json()["detail"] == {"error": "auth_config_error"}

    def test_missing_token_in_login_response_returns_500_config(self, monkeypatch):
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
            AsyncMock(return_value={}),  # no token
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 500
        assert resp.json()["detail"] == {"error": "auth_config_error"}


class TestSignupRouteRequestValidation:
    """Pydantic-level validation returns 422 before FA is called."""

    def test_missing_email_returns_422_without_calling_fa(self, monkeypatch):
        register_mock = AsyncMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", register_mock
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/signup", json={"password": "Password123!"}
        )

        assert resp.status_code == 422
        register_mock.assert_not_called()

    def test_malformed_email_returns_422_without_calling_fa(self, monkeypatch):
        register_mock = AsyncMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", register_mock
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/signup",
            json={"email": "not-an-email", "password": "Password123!"},
        )

        assert resp.status_code == 422
        register_mock.assert_not_called()

    def test_missing_password_returns_422_without_calling_fa(self, monkeypatch):
        register_mock = AsyncMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", register_mock
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/signup", json={"email": "cook@example.com"}
        )

        assert resp.status_code == 422
        register_mock.assert_not_called()
        # The detail envelope from FastAPI's default RequestValidationError
        # handler mentions the missing field — frontend keys off this to
        # render the right inline error.
        body = resp.json()
        rendered = repr(body).lower()
        assert "password" in rendered


# ── U3 contract sentinels: email-lowercasing, ordering,
#    duplicate-registration at route level, FA 5xx, password
#    never logged, response never echoes password. ────────


class TestSignupEmailNormalization:
    """Email submitted mixed-case must reach FA lowercased."""

    def test_lowercases_email_before_calling_fa(self, monkeypatch):
        # Pydantic normalizer does the lowering; this test locks the
        # contract so a future refactor that drops the normalizer fails
        # loudly here instead of silently leaking mixed-case emails to FA.
        register_mock = AsyncMock(return_value={"user": {"id": _FA_USER_ID}})
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", register_mock
        )
        monkeypatch.setattr(
            _signup_mod,
            "upsert_user_mirror",
            MagicMock(return_value=_make_user_row(email="alice@example.com")),
        )
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "login",
            AsyncMock(return_value={"token": "t"}),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/signup",
            json={
                "email": "Alice@Example.COM",
                "password": "Password123!",
                "display_name": "Alice",
            },
        )

        assert resp.status_code == 201, resp.text
        register_mock.assert_awaited_once()
        kwargs = register_mock.await_args.kwargs
        assert kwargs["email"] == "alice@example.com"


class TestSignupHappyPathOrdering:
    """Login MUST happen after the mirror — never before."""

    def test_login_called_after_mirror(self, monkeypatch):
        order: list[str] = []
        session = MagicMock()

        async def _register(**_kwargs):
            order.append("register")
            return {"user": {"id": _FA_USER_ID}}

        def _mirror(*_args, **_kwargs):
            order.append("mirror")
            return _make_user_row()

        async def _login(**_kwargs):
            order.append("login")
            return {"token": "jwt.token.value"}

        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", _register
        )
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", _mirror)
        monkeypatch.setattr(_signup_mod.fusionauth_client, "login", _login)

        client = _build_client_with_session(session)
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 201, resp.text
        # The full pipeline must run register → mirror → login. login
        # before mirror would mean we issue tokens for users that may not
        # exist locally yet — breaks the FK invariant the mirror enforces.
        assert order == ["register", "mirror", "login"]


class TestSignupRouteFaConfigError:
    """Route-level translation of the [duplicate]registration pitfall."""

    def test_duplicate_registration_returns_500_auth_config_error(
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
        mirror_mock = MagicMock()
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", mirror_mock)

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(
            logging.ERROR, logger="app.api.routes.auth_signup"
        ):
            resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 500
        assert resp.json()["detail"] == {"error": "auth_config_error"}
        # Mirror must NOT have been touched — FA register failed.
        mirror_mock.assert_not_called()
        # Caplog must contain a record naming the config-error key so the
        # next on-call has a breadcrumb pointing at the path-arg pitfall.
        assert any(
            "fa_config_error_duplicate_registration" in rec.getMessage()
            for rec in caplog.records
        )


class TestSignupRouteFa5xx:
    """Explicit FA 5xx surface is 503 + no mirror write."""

    def test_fa_5xx_on_register_returns_503(self, monkeypatch):
        # FusionAuthUnavailable with an explicit status_code=503 (vs the
        # transport-error path which has status_code=None). The route
        # must treat both as auth_service_unavailable.
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthUnavailable(
                    status_code=503, body={"error": "upstream"}
                )
            ),
        )
        mirror_mock = MagicMock()
        monkeypatch.setattr(_signup_mod, "upsert_user_mirror", mirror_mock)
        login_mock = AsyncMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "login", login_mock
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "auth_service_unavailable"}
        mirror_mock.assert_not_called()
        login_mock.assert_not_called()


class TestSignupMirrorNoFaCleanup:
    """When the local mirror fails, the route MUST NOT delete the FA user."""

    def test_mirror_failure_does_not_call_any_fa_cleanup(self, monkeypatch):
        # Defense against a future "helpful" refactor that adds a
        # rollback delete-from-FA after a mirror failure. The spec
        # explicitly defers orphan reconciliation to a job; cleanup at
        # request time would race with a retrying client and delete a
        # user from under them.
        register_mock = AsyncMock(return_value={"user": {"id": _FA_USER_ID}})
        login_mock = AsyncMock()
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", register_mock
        )
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "login", login_mock
        )
        monkeypatch.setattr(
            _signup_mod,
            "upsert_user_mirror",
            MagicMock(side_effect=RuntimeError("simulated db failure")),
        )

        client = _build_client_with_session(MagicMock())
        resp = client.post("/api/v1/auth/signup", json=_VALID_PAYLOAD)

        assert resp.status_code == 500
        assert resp.json()["detail"] == {"error": "user_mirror_failed"}
        # register was called exactly once (the creation) and never
        # again — i.e. no FA delete dressed up as a register call.
        register_mock.assert_awaited_once()
        # login must not be attempted after a mirror failure.
        login_mock.assert_not_called()


_SENTINEL_PASSWORD = "hunter2-secret-xyz"


def _payload_with_sentinel_pw() -> dict:
    return {
        "email": "cook@example.com",
        "password": _SENTINEL_PASSWORD,
        "display_name": "Cook",
    }


def _log_contains_sentinel(caplog) -> bool:
    """Walk every captured record and verify the sentinel password is absent.

    Inspects ``record.getMessage()`` (the rendered message), the raw
    ``record.msg`` (in case a future caller logs a dict format-string),
    and every ``record.<extra-key>`` attribute that loggers attach when
    ``extra={...}`` is passed. Returns True iff the sentinel appears
    anywhere — used as the negated assertion.
    """
    for rec in caplog.records:
        try:
            rendered = rec.getMessage()
        except Exception:
            rendered = ""
        if _SENTINEL_PASSWORD in rendered:
            return True
        if _SENTINEL_PASSWORD in repr(rec.msg):
            return True
        # logger.error("...", extra={...}) puts the extras directly on
        # the record as attributes. Walk the record __dict__ so future
        # additions to the extra dict are covered without per-test
        # bookkeeping.
        for attr_name, attr_val in vars(rec).items():
            if attr_name in {"msg", "message", "args"}:
                continue
            if _SENTINEL_PASSWORD in repr(attr_val):
                return True
    return False


class TestSignupNeverLogsPassword:
    """The load-bearing password-redaction sentinel.

    Without this test, a future ``logger.debug(payload.dict())`` would
    silently leak passwords to stdout/journald. The test pumps a known
    sentinel through every code path (happy + each failure branch) and
    asserts the sentinel never appears in any captured log record.
    """

    def test_happy_path_never_logs_password(self, monkeypatch, caplog):
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

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG):
            resp = client.post(
                "/api/v1/auth/signup", json=_payload_with_sentinel_pw()
            )
        assert resp.status_code == 201, resp.text
        assert not _log_contains_sentinel(caplog)

    def test_fa_unavailable_path_never_logs_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(
                side_effect=FusionAuthUnavailable(status_code=None, body=None)
            ),
        )

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG):
            resp = client.post(
                "/api/v1/auth/signup", json=_payload_with_sentinel_pw()
            )
        assert resp.status_code == 503
        assert not _log_contains_sentinel(caplog)

    def test_weak_password_path_never_logs_password(self, monkeypatch, caplog):
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

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG):
            resp = client.post(
                "/api/v1/auth/signup", json=_payload_with_sentinel_pw()
            )
        assert resp.status_code == 400
        assert not _log_contains_sentinel(caplog)

    def test_duplicate_registration_path_never_logs_password(
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

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG):
            resp = client.post(
                "/api/v1/auth/signup", json=_payload_with_sentinel_pw()
            )
        assert resp.status_code == 500
        assert not _log_contains_sentinel(caplog)

    def test_mirror_failure_path_never_logs_password(self, monkeypatch, caplog):
        monkeypatch.setattr(
            _signup_mod.fusionauth_client,
            "register_user",
            AsyncMock(return_value={"user": {"id": _FA_USER_ID}}),
        )
        monkeypatch.setattr(
            _signup_mod,
            "upsert_user_mirror",
            MagicMock(side_effect=RuntimeError("simulated db failure")),
        )

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG):
            resp = client.post(
                "/api/v1/auth/signup", json=_payload_with_sentinel_pw()
            )
        assert resp.status_code == 500
        assert not _log_contains_sentinel(caplog)

    def test_login_failure_path_never_logs_password(self, monkeypatch, caplog):
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
            AsyncMock(
                side_effect=FusionAuthUnavailable(status_code=None, body=None)
            ),
        )

        client = _build_client_with_session(MagicMock())
        with caplog.at_level(logging.DEBUG):
            resp = client.post(
                "/api/v1/auth/signup", json=_payload_with_sentinel_pw()
            )
        assert resp.status_code == 503
        assert not _log_contains_sentinel(caplog)


class TestSignupAlwaysUserRole:
    """A public signup endpoint must NEVER mint an admin token.

    Even if the request payload smuggles in a ``role`` (or
    ``registration``) field, Pydantic's ``SignupRequest`` ignores it and
    the route hardcodes ``roles=['user']`` when calling FA. This test
    fires the payload with the smuggle and verifies the FA call still
    carries the user role.
    """

    def test_smuggled_role_is_ignored(self, monkeypatch):
        register_mock = AsyncMock(return_value={"user": {"id": _FA_USER_ID}})
        monkeypatch.setattr(
            _signup_mod.fusionauth_client, "register_user", register_mock
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

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/signup",
            json={
                "email": "cook@example.com",
                "password": "Password123!",
                "display_name": "Cook",
                "role": "admin",
                "roles": ["admin", "super_admin"],
                "registration": {"roles": ["super_admin"]},
            },
        )

        assert resp.status_code == 201, resp.text
        register_mock.assert_awaited_once()
        kwargs = register_mock.await_args.kwargs
        assert kwargs["roles"] == ["user"]


class TestSignupResponseOmitsPassword:
    """The 201 response body must not contain any password value anywhere."""

    def test_response_does_not_echo_password(self, monkeypatch):
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

        client = _build_client_with_session(MagicMock())
        resp = client.post(
            "/api/v1/auth/signup", json=_payload_with_sentinel_pw()
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()

        def _walk(node) -> None:
            if isinstance(node, dict):
                # No key named "password" anywhere — defends against a
                # future change to AuthResponse / UserOut that leaks the
                # field name even if the value is "***".
                assert "password" not in node, (
                    f"response body unexpectedly contains 'password' key: {node!r}"
                )
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(body)
        # And the sentinel password value must not appear anywhere in
        # the response, even under a renamed key.
        assert _SENTINEL_PASSWORD not in repr(body)
