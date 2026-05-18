"""Unit tests for ``app.services.fusionauth_client.get_jwks``.

The function is the simplest of the three FusionAuth client calls
(no auth header, no body) so its tests pin down the error-mapping
pattern reused by login / register.

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
    _safe_body,
    get_jwks,
)


JWKS_PATH = "/.well-known/jwks.json"
SAMPLE_JWKS = {
    "keys": [
        {
            "kid": "abc-123",
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "n": "0vx7...",
            "e": "AQAB",
        }
    ]
}


def _patch_async_client(monkeypatch, transport: httpx.MockTransport) -> None:
    """Patch httpx.AsyncClient inside the client module to use a MockTransport.

    We can't pass transport= to the real call site (it constructs its
    own client), so swap in a factory that injects the transport.
    """

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(fusionauth_client.httpx, "AsyncClient", _PatchedAsyncClient)


@pytest.mark.unit
class TestSafeBody:
    """``_safe_body`` returns parsed JSON when possible, else raw text."""

    def test_returns_dict_when_body_is_json(self):
        resp = httpx.Response(200, json={"keys": []})
        assert _safe_body(resp) == {"keys": []}

    def test_returns_text_when_body_is_not_json(self):
        resp = httpx.Response(502, text="<html>Bad Gateway</html>")
        assert _safe_body(resp) == "<html>Bad Gateway</html>"

    def test_returns_empty_string_for_empty_body(self):
        resp = httpx.Response(204, content=b"")
        # Empty body is not valid JSON — fallback to text.
        assert _safe_body(resp) == ""


@pytest.mark.unit
class TestGetJwksSuccess:
    """2xx responses return the parsed JSON document verbatim."""

    @pytest.mark.asyncio
    async def test_returns_parsed_jwks_on_200(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=SAMPLE_JWKS)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        result = await get_jwks()
        assert result == SAMPLE_JWKS
        assert captured["method"] == "GET"
        assert captured["url"].endswith(JWKS_PATH)
        assert captured["auth"] is None

    @pytest.mark.asyncio
    async def test_uses_settings_fusionauth_url(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=SAMPLE_JWKS)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await get_jwks()
        assert captured["url"] == f"{settings.fusionauth_url}{JWKS_PATH}"

    @pytest.mark.asyncio
    async def test_returns_empty_keys_when_fa_returns_empty_set(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"keys": []})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        result = await get_jwks()
        assert result == {"keys": []}


@pytest.mark.unit
class TestGetJwksNetworkErrors:
    """``httpx.RequestError`` family maps to FusionAuthUnavailable(None, None)."""

    @pytest.mark.asyncio
    async def test_connect_error_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await get_jwks()
        assert excinfo.value.status_code is None
        assert excinfo.value.body is None
        assert "FA network error" in excinfo.value.message

    @pytest.mark.asyncio
    async def test_read_timeout_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await get_jwks()
        assert excinfo.value.status_code is None
        assert excinfo.value.body is None

    @pytest.mark.asyncio
    async def test_connect_timeout_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connect timed out", request=request)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await get_jwks()
        assert excinfo.value.status_code is None

    @pytest.mark.asyncio
    async def test_network_error_message_includes_exception(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("name resolution failure", request=request)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await get_jwks()
        assert "name resolution failure" in excinfo.value.message


@pytest.mark.unit
class TestGetJwks5xx:
    """5xx responses raise FusionAuthUnavailable with status + body."""

    @pytest.mark.asyncio
    async def test_500_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await get_jwks()
        assert excinfo.value.status_code == 500
        assert excinfo.value.body == {"error": "boom"}

    @pytest.mark.asyncio
    async def test_502_raises_unavailable_with_html_body(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="<html>Bad Gateway</html>")

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await get_jwks()
        assert excinfo.value.status_code == 502
        assert excinfo.value.body == "<html>Bad Gateway</html>"

    @pytest.mark.asyncio
    async def test_503_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "maintenance"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await get_jwks()
        assert excinfo.value.status_code == 503

    @pytest.mark.asyncio
    async def test_504_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(504, json={"error": "gateway timeout"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await get_jwks()
        assert excinfo.value.status_code == 504


@pytest.mark.unit
class TestGetJwks4xx:
    """4xx responses raise FusionAuthValidationError with status + body."""

    @pytest.mark.asyncio
    async def test_400_raises_validation_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad request"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await get_jwks()
        assert excinfo.value.status_code == 400
        assert excinfo.value.body == {"error": "bad request"}

    @pytest.mark.asyncio
    async def test_401_raises_validation_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await get_jwks()
        assert excinfo.value.status_code == 401

    @pytest.mark.asyncio
    async def test_404_raises_validation_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="Not Found")

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await get_jwks()
        assert excinfo.value.status_code == 404
        assert excinfo.value.body == "Not Found"

    @pytest.mark.asyncio
    async def test_validation_error_is_subclass_of_base(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "x"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthError):
            await get_jwks()

    @pytest.mark.asyncio
    async def test_validation_not_raised_as_unavailable(self, monkeypatch):
        """4xx must not be mistakenly mapped to FusionAuthUnavailable."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "x"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError):
            await get_jwks()
