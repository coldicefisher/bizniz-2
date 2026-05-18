"""Unit tests for the FusionAuth HTTP client wrapper.

These tests mock ``httpx.AsyncClient`` at the module-import seam in
``app.services.fusionauth_client`` so they do NOT require a running
FusionAuth instance. They lock in the URL shapes, header shapes,
body shapes, exception mapping, and password-redaction behavior
that downstream routes depend on.

The mock approach (rather than ``respx``) is used because ``respx``
is not currently a dev dependency of the backend service. The
``unittest.mock.patch`` form is more verbose but equally precise: we
assert on the ``call_args`` of the mocked ``AsyncClient`` constructor
and on the ``call_args`` of its ``post`` / ``get`` methods.
"""
# Settings() requires FUSIONAUTH_TENANT_ID even though tenant_id isn't
# referenced by the client. The dev container env is missing it, so we
# fill in safe defaults here BEFORE importing app.core.config (which
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

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.services import fusionauth_client as fa_client
from app.services.fusionauth_client import (
    FusionAuthUnavailable,
    FusionAuthValidationError,
    get_jwks,
    login,
    register_user,
)


class _StubResponse:
    """Tiny stand-in for ``httpx.Response`` used by the AsyncClient mock."""

    def __init__(self, status_code: int, json_body: Optional[Any] = None) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = "" if json_body is None else str(json_body)

    def json(self) -> Any:
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


def _make_async_client_mock(
    *,
    response: Optional[_StubResponse] = None,
    raise_on_request: Optional[BaseException] = None,
):
    """Build a MagicMock that stands in for ``httpx.AsyncClient``.

    Returns a 2-tuple ``(client_class_mock, inner_client_mock)``.
    ``client_class_mock`` is what you patch ``httpx.AsyncClient`` with;
    ``inner_client_mock`` is the object yielded inside
    ``async with httpx.AsyncClient(...) as client:`` so you can assert
    on its ``post`` / ``get`` call args.
    """
    inner = AsyncMock()
    if raise_on_request is not None:
        inner.post = AsyncMock(side_effect=raise_on_request)
        inner.get = AsyncMock(side_effect=raise_on_request)
    else:
        inner.post = AsyncMock(return_value=response)
        inner.get = AsyncMock(return_value=response)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner)
    cm.__aexit__ = AsyncMock(return_value=None)

    client_class = MagicMock(return_value=cm)
    return client_class, inner


@pytest.fixture(autouse=True)
def _fa_settings(monkeypatch):
    """Pin FusionAuth-related settings to known values for every test."""
    monkeypatch.setattr(settings, "fusionauth_url", "http://auth:9011")
    monkeypatch.setattr(
        settings,
        "fusionauth_application_id",
        "85a03867-dccf-4882-adde-1a79aeec50df",
    )
    monkeypatch.setattr(settings, "fusionauth_api_key", "test-api-key-xyz")
    yield


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


async def test_login_posts_correct_body():
    """login() POSTs to /api/login with the contract body and NO auth header."""
    response = _StubResponse(200, {"token": "jwt", "user": {"id": "u1"}})
    client_class, inner = _make_async_client_mock(response=response)

    with patch.object(fa_client.httpx, "AsyncClient", client_class):
        result = await login("a@b.c", "pw")

    assert result == {"token": "jwt", "user": {"id": "u1"}}
    # URL is positional arg 0 to post()
    post_call = inner.post.call_args
    assert post_call.args[0] == "http://auth:9011/api/login"

    body = post_call.kwargs["json"]
    assert body["loginId"] == "a@b.c"
    assert body["password"] == "pw"
    assert body["applicationId"] == settings.fusionauth_application_id

    # NO Authorization header. login is unauthenticated.
    assert "headers" not in post_call.kwargs or (
        "Authorization" not in (post_call.kwargs.get("headers") or {})
    )


async def test_login_4xx_raises_validation():
    """A 4xx response from FA → FusionAuthValidationError with status_code."""
    response = _StubResponse(404, {"error": "not found"})
    client_class, _ = _make_async_client_mock(response=response)

    with patch.object(fa_client.httpx, "AsyncClient", client_class):
        with pytest.raises(FusionAuthValidationError) as exc_info:
            await login("a@b.c", "pw")

    assert exc_info.value.status_code == 404


async def test_login_5xx_raises_unavailable():
    """A 5xx response from FA → FusionAuthUnavailable."""
    response = _StubResponse(503, {"error": "down"})
    client_class, _ = _make_async_client_mock(response=response)

    with patch.object(fa_client.httpx, "AsyncClient", client_class):
        with pytest.raises(FusionAuthUnavailable) as exc_info:
            await login("a@b.c", "pw")

    assert exc_info.value.status_code == 503


async def test_login_network_error_raises_unavailable():
    """A transport-level httpx.ConnectError → FusionAuthUnavailable."""
    client_class, _ = _make_async_client_mock(
        raise_on_request=httpx.ConnectError("connection refused")
    )

    with patch.object(fa_client.httpx, "AsyncClient", client_class):
        with pytest.raises(FusionAuthUnavailable) as exc_info:
            await login("a@b.c", "pw")

    # Transport-level errors carry status_code=None.
    assert exc_info.value.status_code is None


# ---------------------------------------------------------------------------
# register_user
# ---------------------------------------------------------------------------


async def test_register_user_uses_correct_url():
    """register_user() POSTs the contract URL/body and uses the raw API key header.

    This is the contract-pitfall sentinel: locks in the
    no-path-arg ``/api/user/registration`` URL so a future edit cannot
    reintroduce the ``[duplicate]registration`` bug.
    """
    response = _StubResponse(
        200, {"user": {"id": "uuid-new"}, "registration": {"id": "reg-new"}}
    )
    client_class, inner = _make_async_client_mock(response=response)

    with patch.object(fa_client.httpx, "AsyncClient", client_class):
        result = await register_user(
            email="alice@example.com",
            password="Password123!",
            display_name="Alice",
            roles=["user"],
        )

    assert result == {"user": {"id": "uuid-new"}, "registration": {"id": "reg-new"}}

    post_call = inner.post.call_args
    # NO path arg, NO trailing application id.
    assert post_call.args[0] == "http://auth:9011/api/user/registration"

    headers = post_call.kwargs["headers"]
    # Raw API key, NOT 'Bearer ...'.
    assert headers["Authorization"] == settings.fusionauth_api_key
    assert not headers["Authorization"].startswith("Bearer ")

    body = post_call.kwargs["json"]
    assert body["registration"]["applicationId"] == settings.fusionauth_application_id
    assert body["user"]["fullName"] == "Alice"

    # Now: display_name=None ⇒ no fullName key.
    inner.post.reset_mock()
    inner.post.return_value = response

    with patch.object(fa_client.httpx, "AsyncClient", client_class):
        await register_user(
            email="bob@example.com",
            password="Password123!",
            display_name=None,
            roles=["user"],
        )

    body2 = inner.post.call_args.kwargs["json"]
    assert "fullName" not in body2["user"]


async def test_register_user_includes_roles():
    """register_user() forwards the supplied roles list in the registration body."""
    response = _StubResponse(200, {"user": {"id": "uuid"}, "registration": {}})
    client_class, inner = _make_async_client_mock(response=response)

    with patch.object(fa_client.httpx, "AsyncClient", client_class):
        await register_user(
            email="alice@example.com",
            password="Password123!",
            display_name=None,
            roles=["user"],
        )

    body = inner.post.call_args.kwargs["json"]
    assert body["registration"]["roles"] == ["user"]


# ---------------------------------------------------------------------------
# get_jwks
# ---------------------------------------------------------------------------


async def test_get_jwks_no_auth_header():
    """get_jwks() GETs the JWKS URL and does NOT attach an Authorization header."""
    response = _StubResponse(200, {"keys": [{"kid": "k1", "kty": "RSA"}]})
    client_class, inner = _make_async_client_mock(response=response)

    with patch.object(fa_client.httpx, "AsyncClient", client_class):
        result = await get_jwks()

    assert result == {"keys": [{"kid": "k1", "kty": "RSA"}]}

    get_call = inner.get.call_args
    assert get_call.args[0] == "http://auth:9011/.well-known/jwks.json"
    # Either no headers kwarg or no Authorization key in it.
    headers = get_call.kwargs.get("headers") or {}
    assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# Password redaction (sentinel) + timeout
# ---------------------------------------------------------------------------


def test_password_not_in_exception_repr():
    """A FusionAuthValidationError whose body contains a password must NOT
    expose that password in its string repr (verifies _redact).
    """
    body = {
        "fieldErrors": {
            "user.password": [{"code": "[tooShort]"}],
        },
        "user": {"email": "alice@example.com", "password": "hunter2"},
    }
    exc = FusionAuthValidationError(status_code=400, body=body)
    rendered = str(exc)
    assert "hunter2" not in rendered
    assert "***" in rendered  # _redact replaces with '***'


async def test_timeout_is_10s():
    """httpx.AsyncClient is constructed with timeout=10.0 (per auth contract)."""
    response = _StubResponse(200, {"token": "jwt", "user": {}})
    client_class, _ = _make_async_client_mock(response=response)

    with patch.object(fa_client.httpx, "AsyncClient", client_class):
        await login("a@b.c", "pw")

    # Constructor was called with timeout=10.0.
    ctor_call = client_class.call_args
    assert ctor_call.kwargs.get("timeout") == 10.0
