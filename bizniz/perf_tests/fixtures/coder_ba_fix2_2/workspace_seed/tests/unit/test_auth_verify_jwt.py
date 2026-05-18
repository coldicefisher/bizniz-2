"""Unit tests for ``_verify_jwt_signature_and_claims`` (BE-006-U4).

Covers the signature + claim verification step of the JWT pipeline:

* Missing ``kid`` in the unverified header → 401 ``invalid_token``.
* :class:`FusionAuthUnavailable` from the cold-cache JWKS fetch →
  503 ``auth_service_unavailable``.
* No JWK in the JWKS whose ``kid`` matches the header → 401
  ``invalid_token`` (with a WARN log).
* :class:`ExpiredSignatureError` from ``jose.jwt.decode`` → 401
  ``token_expired`` (distinct from generic ``invalid_token`` so the
  SPA can offer a "session expired" UX).
* :class:`JWTClaimsError` (wrong aud / iss) → 401 ``invalid_token``.
* :class:`JWTError` (signature failure) → 401 ``invalid_token``.
* Happy path returns the decoded claims dict and passes the expected
  ``algorithms=['RS256']``, ``audience``, ``issuer``, and ``options``
  (including ``leeway`` from ``settings.jwt_leeway_seconds``) through
  to ``jose.jwt.decode``.

Both JWKS retrieval and the inner ``jose.jwt.decode`` call are
monkeypatched at the module seam so these tests run without any
network or live JWT material.
"""
import asyncio
import logging

import pytest
from fastapi import HTTPException
from jose import JWTError
from jose.exceptions import ExpiredSignatureError, JWTClaimsError

from app.core import auth as auth_module
from app.core.auth import _verify_jwt_signature_and_claims
from app.services.fusionauth_client import FusionAuthUnavailable


KID = "kid-test"
OTHER_KID = "kid-other"
JWKS_WITH_KID = {
    "keys": [
        {"kid": KID, "kty": "RSA", "alg": "RS256", "n": "x", "e": "AQAB"},
    ]
}
JWKS_WITHOUT_KID = {
    "keys": [
        {"kid": OTHER_KID, "kty": "RSA", "alg": "RS256", "n": "x", "e": "AQAB"},
    ]
}
SAMPLE_TOKEN = "header.payload.signature"
SAMPLE_HEADER = {"alg": "RS256", "kid": KID}
SAMPLE_CLAIMS = {
    "sub": "00000000-0000-0000-0000-000000000001",
    "email": "user@example.com",
    "roles": ["user"],
    "iss": "acme.com",
    "aud": "85a03867-dccf-4882-adde-1a79aeec50df",
    "exp": 9999999999,
}


@pytest.fixture(autouse=True)
def _reset_cache_and_lock():
    """Cold cache + fresh asyncio.Lock per test (rebinds to the test loop)."""
    auth_module._reset_jwks_cache_for_tests()
    auth_module._jwks_lock = asyncio.Lock()
    yield
    auth_module._reset_jwks_cache_for_tests()


def _patch_jwks(monkeypatch, result):
    """Patch ``_get_jwks_with_refresh`` to return ``result`` (or raise it)."""
    async def _fake(kid: str):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(auth_module, "_get_jwks_with_refresh", _fake)


def _patch_decode(monkeypatch, result, capture: dict | None = None):
    """Patch ``jose.jwt.decode`` (as referenced inside ``auth_module``).

    ``result`` is either a dict (returned) or an Exception (raised).
    ``capture`` — when supplied — is populated with the kwargs the
    function was called with so the test can assert on them.
    """
    def _fake(token, key, **kwargs):
        if capture is not None:
            capture["token"] = token
            capture["key"] = key
            capture.update(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(auth_module.jwt, "decode", _fake)


# ── Missing kid ──────────────────────────────────────────


@pytest.mark.unit
class TestMissingKid:
    @pytest.mark.asyncio
    async def test_no_kid_in_header_raises_invalid_token(self, monkeypatch):
        # Should fail BEFORE attempting any JWKS fetch.
        _patch_jwks(
            monkeypatch,
            AssertionError("_get_jwks_with_refresh should not be called"),
        )

        with pytest.raises(HTTPException) as exc:
            await _verify_jwt_signature_and_claims(SAMPLE_TOKEN, {"alg": "RS256"})

        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    @pytest.mark.asyncio
    async def test_empty_kid_raises_invalid_token(self, monkeypatch):
        _patch_jwks(
            monkeypatch,
            AssertionError("_get_jwks_with_refresh should not be called"),
        )
        with pytest.raises(HTTPException) as exc:
            await _verify_jwt_signature_and_claims(
                SAMPLE_TOKEN, {"alg": "RS256", "kid": ""}
            )
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}


# ── FA unavailable on cold cache ─────────────────────────


@pytest.mark.unit
class TestFusionAuthUnavailable:
    @pytest.mark.asyncio
    async def test_unavailable_translates_to_503(self, monkeypatch):
        _patch_jwks(
            monkeypatch,
            FusionAuthUnavailable(status_code=None, body=None, message="down"),
        )

        with pytest.raises(HTTPException) as exc:
            await _verify_jwt_signature_and_claims(SAMPLE_TOKEN, SAMPLE_HEADER)

        assert exc.value.status_code == 503
        assert exc.value.detail == {"error": "auth_service_unavailable"}

    @pytest.mark.asyncio
    async def test_unavailable_5xx_translates_to_503(self, monkeypatch):
        _patch_jwks(
            monkeypatch,
            FusionAuthUnavailable(status_code=503, body={"err": "x"}),
        )

        with pytest.raises(HTTPException) as exc:
            await _verify_jwt_signature_and_claims(SAMPLE_TOKEN, SAMPLE_HEADER)

        assert exc.value.status_code == 503
        assert exc.value.detail == {"error": "auth_service_unavailable"}


# ── kid not found in JWKS ────────────────────────────────


@pytest.mark.unit
class TestKidNotInJwks:
    @pytest.mark.asyncio
    async def test_no_matching_kid_raises_invalid_token(
        self, monkeypatch, caplog
    ):
        _patch_jwks(monkeypatch, JWKS_WITHOUT_KID)

        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            with pytest.raises(HTTPException) as exc:
                await _verify_jwt_signature_and_claims(
                    SAMPLE_TOKEN, SAMPLE_HEADER
                )

        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}
        # The warning surfaces the missed kid for ops debugging.
        assert any(
            "jwks_kid_not_found" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_empty_keys_array_raises_invalid_token(self, monkeypatch):
        _patch_jwks(monkeypatch, {"keys": []})

        with pytest.raises(HTTPException) as exc:
            await _verify_jwt_signature_and_claims(SAMPLE_TOKEN, SAMPLE_HEADER)
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    @pytest.mark.asyncio
    async def test_jwks_missing_keys_field_raises_invalid_token(
        self, monkeypatch
    ):
        # Defensive against ``{}`` or ``{"keys": null}`` from a buggy FA.
        _patch_jwks(monkeypatch, {})

        with pytest.raises(HTTPException) as exc:
            await _verify_jwt_signature_and_claims(SAMPLE_TOKEN, SAMPLE_HEADER)
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}


# ── jwt.decode exception mapping ─────────────────────────


@pytest.mark.unit
class TestDecodeExceptionMapping:
    @pytest.mark.asyncio
    async def test_expired_signature_maps_to_token_expired(self, monkeypatch):
        _patch_jwks(monkeypatch, JWKS_WITH_KID)
        _patch_decode(monkeypatch, ExpiredSignatureError("expired"))

        with pytest.raises(HTTPException) as exc:
            await _verify_jwt_signature_and_claims(SAMPLE_TOKEN, SAMPLE_HEADER)

        # token_expired MUST be distinct from invalid_token so the SPA
        # can offer a "session expired, please log in again" prompt.
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "token_expired"}

    @pytest.mark.asyncio
    async def test_jwt_claims_error_maps_to_invalid_token(self, monkeypatch):
        _patch_jwks(monkeypatch, JWKS_WITH_KID)
        _patch_decode(monkeypatch, JWTClaimsError("wrong audience"))

        with pytest.raises(HTTPException) as exc:
            await _verify_jwt_signature_and_claims(SAMPLE_TOKEN, SAMPLE_HEADER)

        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    @pytest.mark.asyncio
    async def test_generic_jwt_error_maps_to_invalid_token(self, monkeypatch):
        _patch_jwks(monkeypatch, JWKS_WITH_KID)
        _patch_decode(monkeypatch, JWTError("bad signature"))

        with pytest.raises(HTTPException) as exc:
            await _verify_jwt_signature_and_claims(SAMPLE_TOKEN, SAMPLE_HEADER)

        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}


# ── Happy path ───────────────────────────────────────────


@pytest.mark.unit
class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_decoded_claims(self, monkeypatch):
        _patch_jwks(monkeypatch, JWKS_WITH_KID)
        _patch_decode(monkeypatch, SAMPLE_CLAIMS)

        claims = await _verify_jwt_signature_and_claims(
            SAMPLE_TOKEN, SAMPLE_HEADER
        )
        assert claims == SAMPLE_CLAIMS

    @pytest.mark.asyncio
    async def test_decode_called_with_correct_kwargs(self, monkeypatch):
        _patch_jwks(monkeypatch, JWKS_WITH_KID)
        captured: dict = {}
        _patch_decode(monkeypatch, SAMPLE_CLAIMS, capture=captured)

        await _verify_jwt_signature_and_claims(SAMPLE_TOKEN, SAMPLE_HEADER)

        # token and matching JWK forwarded verbatim.
        assert captured["token"] == SAMPLE_TOKEN
        assert captured["key"] == JWKS_WITH_KID["keys"][0]
        # RS256 pin enforced at decode time (defense-in-depth, even
        # though _decode_unverified_header already pinned alg upstream).
        assert captured["algorithms"] == ["RS256"]
        assert captured["audience"] == auth_module.settings.fusionauth_application_id
        assert captured["issuer"] == auth_module.settings.fusionauth_issuer
        # leeway lives inside the options dict in python-jose 3.x.
        options = captured["options"]
        assert options["leeway"] == auth_module.settings.jwt_leeway_seconds
        assert options["require_aud"] is True
        assert options["require_iss"] is True
        assert options["require_exp"] is True

    @pytest.mark.asyncio
    async def test_matching_key_picked_when_multiple_present(self, monkeypatch):
        # Confirm the function picks the key whose kid matches the
        # header rather than just the first key in the array.
        jwks = {
            "keys": [
                {"kid": OTHER_KID, "kty": "RSA", "n": "a", "e": "AQAB"},
                {"kid": KID, "kty": "RSA", "n": "b", "e": "AQAB"},
            ]
        }
        _patch_jwks(monkeypatch, jwks)
        captured: dict = {}
        _patch_decode(monkeypatch, SAMPLE_CLAIMS, capture=captured)

        await _verify_jwt_signature_and_claims(SAMPLE_TOKEN, SAMPLE_HEADER)
        assert captured["key"]["kid"] == KID
