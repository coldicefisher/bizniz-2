"""Unit tests for _validate_token_shape and _decode_unverified_header.

Covers the helpers added in BE-006-U3:

- ``_validate_token_shape``: structural checks on the Authorization
  header (presence, ``Bearer`` prefix, three dot-separated segments)
  with the 401-code split between ``'unauthenticated'`` (no/empty
  credentials) and ``'invalid_token'`` (malformed).
- ``_decode_unverified_header``: algorithm pinning. Parses the JWT
  header without verification and rejects anything that is not
  ``alg=RS256`` — the cheap defense against ``alg=none`` and
  HS-with-public-key downgrade attacks.

The tests use the ``python-jose`` library to construct real
unsigned headers (so the decoder actually parses base64url) instead
of mocking ``jwt.get_unverified_header``. That way the test fails
if the import chain or the library contract changes.
"""
import base64
import json
import logging

import pytest
from fastapi import HTTPException

from app.core.auth import _decode_unverified_header, _validate_token_shape


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding (matches JWT format)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_token(header: dict, payload: dict | None = None) -> str:
    """Build a syntactically valid 3-segment JWT with the given header."""
    payload = payload or {"sub": "test"}
    h = _b64url(json.dumps(header).encode("utf-8"))
    p = _b64url(json.dumps(payload).encode("utf-8"))
    s = _b64url(b"signature")
    return f"{h}.{p}.{s}"


# ── _validate_token_shape ────────────────────────────────


class TestValidateTokenShapeMissing:
    def test_none_raises_unauthenticated(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape(None)  # type: ignore[arg-type]
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "unauthenticated"}

    def test_empty_string_raises_unauthenticated(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape("")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "unauthenticated"}


class TestValidateTokenShapeBadScheme:
    def test_no_bearer_prefix(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape("Basic abc.def.ghi")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "unauthenticated"}

    def test_lowercase_bearer_accepted(self) -> None:
        # RFC 6750 §2.1: the ``Bearer`` scheme name is matched
        # case-insensitively. Lowercase / uppercase / mixed-case all
        # produce the same bare-token return as the canonical
        # ``Bearer`` spelling.
        result = _validate_token_shape("bearer abc.def.ghi")
        assert result == "abc.def.ghi"

    def test_bearer_without_space(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape("Bearerabc.def.ghi")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "unauthenticated"}

    def test_just_random_text(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape("random nonsense")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "unauthenticated"}


class TestValidateTokenShapeEmptyToken:
    def test_bearer_then_nothing(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape("Bearer ")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "unauthenticated"}

    def test_bearer_only_whitespace(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape("Bearer    ")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "unauthenticated"}


class TestValidateTokenShapeWrongSegmentCount:
    def test_single_segment(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape("Bearer abc")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    def test_two_segments(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape("Bearer abc.def")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    def test_four_segments(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_token_shape("Bearer abc.def.ghi.jkl")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}


class TestValidateTokenShapeHappyPath:
    def test_three_segments_returned(self) -> None:
        result = _validate_token_shape("Bearer abc.def.ghi")
        assert result == "abc.def.ghi"

    def test_extra_whitespace_trimmed(self) -> None:
        # ``Bearer   abc.def.ghi`` — token strip() removes leading
        # spaces between scheme and value, then strip() also trims
        # trailing whitespace.
        result = _validate_token_shape("Bearer   abc.def.ghi  ")
        assert result == "abc.def.ghi"

    def test_realistic_looking_jwt(self) -> None:
        tok = _make_token({"alg": "RS256", "kid": "x"})
        result = _validate_token_shape(f"Bearer {tok}")
        assert result == tok


# ── _decode_unverified_header ────────────────────────────


class TestDecodeUnverifiedHeaderHappyPath:
    def test_rs256_header_returned(self) -> None:
        tok = _make_token({"alg": "RS256", "kid": "abc"})
        header = _decode_unverified_header(tok)
        assert header["alg"] == "RS256"
        assert header["kid"] == "abc"


class TestDecodeUnverifiedHeaderMalformed:
    def test_garbage_raises_invalid_token(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _decode_unverified_header("not-a-real-jwt")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    def test_non_base64_header_raises_invalid_token(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _decode_unverified_header("$$$.$$$.$$$")
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}


class TestDecodeUnverifiedHeaderAlgPinning:
    def test_alg_none_rejected(self, caplog: pytest.LogCaptureFixture) -> None:
        tok = _make_token({"alg": "none"})
        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            with pytest.raises(HTTPException) as exc:
                _decode_unverified_header(tok)
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}
        assert any("unexpected_alg" in r.message for r in caplog.records)

    def test_hs256_rejected(self, caplog: pytest.LogCaptureFixture) -> None:
        tok = _make_token({"alg": "HS256"})
        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            with pytest.raises(HTTPException) as exc:
                _decode_unverified_header(tok)
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}
        assert any("unexpected_alg" in r.message for r in caplog.records)

    def test_hs384_rejected(self) -> None:
        tok = _make_token({"alg": "HS384"})
        with pytest.raises(HTTPException) as exc:
            _decode_unverified_header(tok)
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    def test_hs512_rejected(self) -> None:
        tok = _make_token({"alg": "HS512"})
        with pytest.raises(HTTPException) as exc:
            _decode_unverified_header(tok)
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    def test_missing_alg_rejected(self) -> None:
        tok = _make_token({"kid": "abc"})  # no alg at all
        with pytest.raises(HTTPException) as exc:
            _decode_unverified_header(tok)
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}

    def test_rs384_rejected_strict_rs256_pinning(self) -> None:
        # We pin RS256 specifically, not the whole RS family.
        tok = _make_token({"alg": "RS384"})
        with pytest.raises(HTTPException) as exc:
            _decode_unverified_header(tok)
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error": "invalid_token"}


class TestPipelineIntegration:
    """``_validate_token_shape`` feeds ``_decode_unverified_header``."""

    def test_full_happy_path(self) -> None:
        tok = _make_token({"alg": "RS256", "kid": "k1"})
        bare = _validate_token_shape(f"Bearer {tok}")
        header = _decode_unverified_header(bare)
        assert header["alg"] == "RS256"
        assert header["kid"] == "k1"

    def test_shape_passes_but_alg_attack_caught(self) -> None:
        # An attacker can construct a syntactically valid 3-segment
        # token with alg=none — shape passes, alg pinning catches it.
        tok = _make_token({"alg": "none"})
        bare = _validate_token_shape(f"Bearer {tok}")
        with pytest.raises(HTTPException) as exc:
            _decode_unverified_header(bare)
        assert exc.value.detail == {"error": "invalid_token"}
