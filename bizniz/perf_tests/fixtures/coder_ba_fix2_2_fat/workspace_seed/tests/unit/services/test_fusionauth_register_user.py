"""Unit tests for ``app.services.fusionauth_client.register_user``.

These tests pin the contract for the FA create-and-register call:

* request is POSTed to ``{FUSIONAUTH_URL}/api/user/registration`` with
  NO userId path arg (the contract pitfall — putting the application
  id there yields ``[duplicate]registration`` 400),
* the ``Authorization`` header is the RAW API key (not ``Bearer ...``),
* body shape carries ``user`` (with optional ``fullName``) and
  ``registration`` (with ``applicationId`` + ``roles``),
* status-code mapping matches the rest of the client (transport/5xx →
  ``FusionAuthUnavailable``, 4xx → ``FusionAuthValidationError``,
  2xx → parsed JSON), and
* the plaintext password never leaks into the stringified exception.

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
    register_user,
)


REGISTRATION_PATH = "/api/user/registration"
SAMPLE_REGISTRATION_RESPONSE = {
    "user": {
        "id": "11111111-2222-3333-4444-555555555555",
        "email": "cook@example.com",
    },
    "registration": {
        "applicationId": "85a03867-dccf-4882-adde-1a79aeec50df",
        "roles": ["user"],
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
class TestRegisterUserSuccess:
    """2xx responses return the parsed JSON envelope verbatim."""

    @pytest.mark.asyncio
    async def test_returns_parsed_json_on_200(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=SAMPLE_REGISTRATION_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        result = await register_user(
            "cook@example.com", "Password123!", "Cookie Monster", ["user"]
        )
        assert result == SAMPLE_REGISTRATION_RESPONSE

    @pytest.mark.asyncio
    async def test_posts_to_no_path_arg_url(self, monkeypatch):
        """The contract pitfall: URL must NOT include {userId}."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(200, json=SAMPLE_REGISTRATION_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await register_user("cook@example.com", "Password123!", None, ["user"])
        assert captured["method"] == "POST"
        assert (
            captured["url"]
            == f"{settings.fusionauth_url}{REGISTRATION_PATH}"
        )
        # Defensive: ensure no extra path segment after /registration
        assert not captured["url"].rstrip("/").endswith("/registration/")
        assert captured["url"].count("/api/user/registration") == 1

    @pytest.mark.asyncio
    async def test_authorization_header_is_raw_api_key(self, monkeypatch):
        """FA admin endpoints use the raw API key — NOT ``Bearer <key>``."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=SAMPLE_REGISTRATION_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await register_user("cook@example.com", "Password123!", None, ["user"])
        assert captured["auth"] == settings.fusionauth_api_key
        assert captured["auth"] is not None
        assert not captured["auth"].lower().startswith("bearer ")

    @pytest.mark.asyncio
    async def test_body_with_display_name(self, monkeypatch):
        import json as _json

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            captured["content_type"] = request.headers.get("content-type")
            return httpx.Response(200, json=SAMPLE_REGISTRATION_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await register_user(
            "cook@example.com", "Password123!", "Cookie Monster", ["user"]
        )
        assert captured["body"] == {
            "user": {
                "email": "cook@example.com",
                "password": "Password123!",
                "fullName": "Cookie Monster",
            },
            "registration": {
                "applicationId": settings.fusionauth_application_id,
                "roles": ["user"],
            },
        }
        assert captured["content_type"] == "application/json"

    @pytest.mark.asyncio
    async def test_body_omits_full_name_when_display_name_is_none(
        self, monkeypatch
    ):
        import json as _json

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=SAMPLE_REGISTRATION_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await register_user("cook@example.com", "Password123!", None, ["user"])
        assert "fullName" not in captured["body"]["user"]
        assert captured["body"]["user"] == {
            "email": "cook@example.com",
            "password": "Password123!",
        }

    @pytest.mark.asyncio
    async def test_body_omits_full_name_when_display_name_is_empty_string(
        self, monkeypatch
    ):
        import json as _json

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=SAMPLE_REGISTRATION_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await register_user("cook@example.com", "Password123!", "", ["user"])
        assert "fullName" not in captured["body"]["user"]

    @pytest.mark.asyncio
    async def test_roles_passed_through_verbatim(self, monkeypatch):
        import json as _json

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=SAMPLE_REGISTRATION_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await register_user(
            "admin@example.com", "Password123!", None, ["admin", "user"]
        )
        assert captured["body"]["registration"]["roles"] == ["admin", "user"]

    @pytest.mark.asyncio
    async def test_empty_roles_list_passed_through(self, monkeypatch):
        import json as _json

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=SAMPLE_REGISTRATION_RESPONSE)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        await register_user("cook@example.com", "Password123!", None, [])
        assert captured["body"]["registration"]["roles"] == []


@pytest.mark.unit
class TestRegisterUserNetworkErrors:
    """``httpx.RequestError`` family maps to FusionAuthUnavailable(None, None)."""

    @pytest.mark.asyncio
    async def test_connect_error_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await register_user("cook@example.com", "Password123!", None, ["user"])
        assert excinfo.value.status_code is None
        assert excinfo.value.body is None
        assert "FA network error" in excinfo.value.message

    @pytest.mark.asyncio
    async def test_read_timeout_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await register_user("cook@example.com", "Password123!", None, ["user"])
        assert excinfo.value.status_code is None

    @pytest.mark.asyncio
    async def test_connect_timeout_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connect timed out", request=request)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await register_user("cook@example.com", "Password123!", None, ["user"])
        assert excinfo.value.status_code is None


@pytest.mark.unit
class TestRegisterUser5xx:
    """5xx responses raise FusionAuthUnavailable with status + body."""

    @pytest.mark.asyncio
    async def test_500_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await register_user("cook@example.com", "Password123!", None, ["user"])
        assert excinfo.value.status_code == 500
        assert excinfo.value.body == {"error": "boom"}

    @pytest.mark.asyncio
    async def test_502_raises_unavailable_with_text_body(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="<html>Bad Gateway</html>")

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await register_user("cook@example.com", "Password123!", None, ["user"])
        assert excinfo.value.status_code == 502
        assert excinfo.value.body == "<html>Bad Gateway</html>"

    @pytest.mark.asyncio
    async def test_503_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "maintenance"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await register_user("cook@example.com", "Password123!", None, ["user"])
        assert excinfo.value.status_code == 503

    @pytest.mark.asyncio
    async def test_504_raises_unavailable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(504, json={"error": "gateway timeout"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await register_user("cook@example.com", "Password123!", None, ["user"])
        assert excinfo.value.status_code == 504


@pytest.mark.unit
class TestRegisterUser4xx:
    """4xx responses raise FusionAuthValidationError with status + body.

    Caller decides what each specific 4xx means (400
    ``[duplicate]user.email`` → 409 email_already_registered, 400
    ``fieldErrors.user.password`` → 400 weak_password, 400
    ``[duplicate]registration`` → 500 auth_config_error).
    """

    @pytest.mark.asyncio
    async def test_400_weak_password_raises_validation_error(self, monkeypatch):
        weak_body = {
            "fieldErrors": {
                "user.password": [
                    {"code": "[tooShort]user.password", "message": "too short"}
                ]
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json=weak_body)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await register_user("cook@example.com", "pw", None, ["user"])
        assert excinfo.value.status_code == 400
        assert excinfo.value.body == weak_body

    @pytest.mark.asyncio
    async def test_400_duplicate_email_raises_validation_error(self, monkeypatch):
        dup_body = {
            "fieldErrors": {
                "user.email": [
                    {"code": "[duplicate]user.email", "message": "already exists"}
                ]
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json=dup_body)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await register_user(
                "existing@example.com", "Password123!", None, ["user"]
            )
        assert excinfo.value.status_code == 400
        assert excinfo.value.body == dup_body

    @pytest.mark.asyncio
    async def test_400_duplicate_registration_raises_validation_error(
        self, monkeypatch
    ):
        """The pitfall code — surfaces as a 4xx so routes can map to
        500 auth_config_error (a backend bug, not a user error)."""
        pitfall_body = {
            "fieldErrors": {
                "registration": [
                    {
                        "code": "[duplicate]registration",
                        "message": "User is already registered",
                    }
                ]
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json=pitfall_body)

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await register_user("cook@example.com", "Password123!", None, ["user"])
        assert excinfo.value.status_code == 400
        assert excinfo.value.body == pitfall_body

    @pytest.mark.asyncio
    async def test_401_raises_validation_error(self, monkeypatch):
        """A bad/missing API key surfaces as 401 — still a 4xx category."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="")

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await register_user("cook@example.com", "Password123!", None, ["user"])
        assert excinfo.value.status_code == 401

    @pytest.mark.asyncio
    async def test_validation_error_is_subclass_of_base(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "x"})

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthError):
            await register_user("cook@example.com", "Password123!", None, ["user"])


@pytest.mark.unit
class TestRegisterUserPasswordRedaction:
    """The plaintext password must never appear in the stringified exception.

    FA error envelopes can echo the request body; the ``_redact`` helper
    ensures even that case never leaks the password into logs.
    """

    @pytest.mark.asyncio
    async def test_password_redacted_in_error_str(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "user": {
                        "email": "cook@example.com",
                        "password": "super-secret-pw",
                    },
                    "fieldErrors": {"user.email": [{"code": "x"}]},
                },
            )

        _patch_async_client(monkeypatch, httpx.MockTransport(handler))
        with pytest.raises(FusionAuthValidationError) as excinfo:
            await register_user(
                "cook@example.com", "super-secret-pw", None, ["user"]
            )
        rendered = str(excinfo.value)
        assert "super-secret-pw" not in rendered
        assert "***" in rendered
