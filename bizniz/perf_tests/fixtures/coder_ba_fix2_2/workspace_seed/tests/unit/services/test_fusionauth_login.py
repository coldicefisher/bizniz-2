"""Unit tests for ``app.services.fusionauth_client.login``.

Login is the second of three FusionAuth client calls. These tests
pin the contract:

* request is POSTed to ``{FUSIONAUTH_URL}/api/login`` with
  ``loginId`` + ``password`` + ``applicationId`` in the JSON body
  and NO ``Authorization`` header,
* status-code mapping matches :func:`get_jwks` (transport/5xx →
  ``FusionAuthUnavailable``, 4xx → ``FusionAuthValidationError``,
  2xx → parsed JSON), and
* the plaintext password never leaks into the stringified
  exception (the ``_redact`` helper is exercised end-to-end).

The HTTP layer is mocked via ``httpx.MockTransport`` so these tests
do NOT require a running FusionAuth instance.
"""
import httpx
import pytest

from app.core.config import settings
from app.services import fusionauth_client
from app.services.fusionauth_client import (
    FusionAuthError,
    FusionAuthUnavailable,
    FusionAuthValidationError,
    login,
)


LOGIN_PATH = "/api/login"
SAMPLE_TOKEN_RESPONSE = {
    "token": "header.payload.signature",
    "user": {
        "id": "11111111-2222-3333-4444-555555555555",
        "email": "user@example.com",
    },
}


def _patch_async_client(monkeypatch, transport: httpx.MockTransport) -> None:
    """Patch httpx.AsyncClient inside the client module to use a MockTransport."""

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(fusionauth_client.httpx, "AsyncClient", _PatchedAsyncClient)


@pytest.mark.unit
class TestLoginSuccess:
    """2xx responses return the parsed JSON envelope verbatim."""

    @pytest.mark.asyncio
    async def test_returns_parsed_json_on_200(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=SAMPLE_TOKEN_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        result = await login("user@example.com", "password")
        assert result == SAMPLE_TOKEN_RESPONSE

    @pytest.mark.asyncio
    async def test_posts_to_correct_url(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(200, json=SAMPLE_TOKEN_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await login("user@example.com", "password")
        assert captured["method"] == "POST"
        assert captured["url"] == f"{settings.fusionauth_url}{LOGIN_PATH}"

    @pytest.mark.asyncio
    async def test_no_authorization_header(self, monkeypatch):
        """FA /api/login is unauthenticated — no API key required."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=SAMPLE_TOKEN_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await login("user@example.com", "password")
        assert captured["auth"] is None

    @pytest.mark.asyncio
    async def test_body_carries_login_id_password_and_app_id(self, monkeypatch):
        import json as _json

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            captured["content_type"] = request.headers.get("content-type")
            return httpx.Response(200, json=SAMPLE_TOKEN_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await login("user@example.com", "Password123!")
        assert captured["body"] == {
            "loginId": "user@example.com",
            "password": "Password123!",
            "applicationId": settings.fusionauth_application_id,
        }
        # httpx sets Content-Type: application/json automatically when json=...
        assert captured["content_type"] == "application/json"

    @pytest.mark.asyncio
    async def test_202_two_factor_required_returns_body(self, monkeypatch):
        """FA returns 202 when 2FA is required — body is still JSON."""
        two_factor_body = {"twoFactorId": "abc", "methods": [{"method": "email"}]}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(202, json=two_factor_body)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        result = await login("user@example.com", "password")
        assert result == two_factor_body

    @pytest.mark.asyncio
    async def test_203_change_password_returns_body(self, monkeypatch):
        """FA returns 203 when the user must change their password."""
        change_pw_body = {"changePasswordId": "xyz"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(203, json=change_pw_body)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        result = await login("user@example.com", "password")
        assert result == change_pw_body


@pytest.mark.unit
class TestLoginNetworkErrors:
    """``httpx.RequestError`` family maps to FusionAuthUnavailable(None, None)."""

    @pytest.mark.asyncio
    async def test_connect_error_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await login("user@example.com", "password")
        assert excinfo.value.status_code is None
        assert excinfo.value.body is None
        assert "FA network error" in excinfo.value.message

    @pytest.mark.asyncio
    async def test_read_timeout_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await login("user@example.com", "password")
        assert excinfo.value.status_code is None

    @pytest.mark.asyncio
    async def test_connect_timeout_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connect timed out", request=request)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await login("user@example.com", "password")
        assert excinfo.value.status_code is None


@pytest.mark.unit
class TestLogin5xx:
    """5xx responses raise FusionAuthUnavailable with status + body."""

    @pytest.mark.asyncio
    async def test_500_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await login("user@example.com", "password")
        assert excinfo.value.status_code == 500
        assert excinfo.value.body == {"error": "boom"}

    @pytest.mark.asyncio
    async def test_502_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="<html>Bad Gateway</html>")

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await login("user@example.com", "password")
        assert excinfo.value.status_code == 502
        assert excinfo.value.body == "<html>Bad Gateway</html>"

    @pytest.mark.asyncio
    async def test_503_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "maintenance"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await login("user@example.com", "password")
        assert excinfo.value.status_code == 503

    @pytest.mark.asyncio
    async def test_504_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(504, json={"error": "gateway timeout"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await login("user@example.com", "password")
        assert excinfo.value.status_code == 504


@pytest.mark.unit
class TestLogin4xx:
    """4xx responses raise FusionAuthValidationError with status + body.

    Caller decides what each specific 4xx means (404 invalid creds,
    423 locked, 400 weak password, etc).
    """

    @pytest.mark.asyncio
    async def test_400_raises_validation_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad request"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await login("user@example.com", "password")
        assert excinfo.value.status_code == 400
        assert excinfo.value.body == {"error": "bad request"}

    @pytest.mark.asyncio
    async def test_404_invalid_credentials_raises_validation_error(
        self, monkeypatch
    ):
        """FA returns 404 for both unknown-email AND wrong-password."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="")

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await login("nobody@example.com", "wrong")
        assert excinfo.value.status_code == 404

    @pytest.mark.asyncio
    async def test_423_account_locked_raises_validation_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(423, json={"error": "locked"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await login("user@example.com", "password")
        assert excinfo.value.status_code == 423
        assert excinfo.value.body == {"error": "locked"}

    @pytest.mark.asyncio
    async def test_validation_error_is_subclass_of_base(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "x"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthError):
            await login("user@example.com", "password")


@pytest.mark.unit
class TestLoginPasswordRedaction:
    """The plaintext password must never appear in the stringified exception.

    The ``_redact`` helper is the safety net for any body the caller
    or a logger stringifies. Login bodies sometimes echo back in
    error envelopes; even if FA never does, we test the guarantee.
    """

    @pytest.mark.asyncio
    async def test_password_redacted_in_error_str(self, monkeypatch):
        """A 4xx body that echoes 'password' must redact it in __str__."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "loginId": "user@example.com",
                    "password": "super-secret-pw",
                    "applicationId": "abc",
                },
            )

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await login("user@example.com", "super-secret-pw")
        rendered = str(excinfo.value)
        assert "super-secret-pw" not in rendered
        assert "***" in rendered
