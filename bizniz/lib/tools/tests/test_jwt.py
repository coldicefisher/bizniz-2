"""Tests for jwt tool factory."""
import base64
import json

import pytest

from bizniz.lib.tools.jwt import build_jwt_handlers, make_decode_jwt


def _make_jwt(header: dict, payload: dict, sig: str = "sigsig") -> str:
    """Build a JWT-like string. Signature is whatever — decode never
    verifies."""
    def enc(d: dict) -> str:
        raw = json.dumps(d, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{enc(header)}.{enc(payload)}.{sig}"


class TestDecodeJwt:
    def test_decodes_header_and_payload(self):
        token = _make_jwt(
            {"alg": "RS256", "kid": "k1", "typ": "JWT"},
            {"sub": "u1", "iss": "https://fa", "roles": ["admin"]},
        )
        out = make_decode_jwt()({"token": token})
        assert "RS256" in out
        assert "u1" in out
        assert "admin" in out
        assert "https://fa" in out
        assert "NOT verified" in out

    def test_strips_bearer_prefix(self):
        token = _make_jwt({"alg": "RS256"}, {"sub": "u1"})
        out = make_decode_jwt()({"token": f"Bearer {token}"})
        assert "u1" in out

    def test_strips_bearer_prefix_case_insensitive(self):
        token = _make_jwt({"alg": "RS256"}, {"sub": "u1"})
        out = make_decode_jwt()({"token": f"BEARER {token}"})
        assert "u1" in out

    def test_empty_token(self):
        out = make_decode_jwt()({"token": ""})
        assert "ERROR" in out

    def test_missing_token_field(self):
        out = make_decode_jwt()({})
        assert "ERROR" in out

    def test_wrong_part_count(self):
        out = make_decode_jwt()({"token": "only.two"})
        assert "expected 3 parts" in out
        assert "got 2" in out

    def test_undecodable_segment(self):
        out = make_decode_jwt()({"token": "!!!.@@@.###"})
        assert "could not decode" in out

    def test_payload_with_unicode(self):
        token = _make_jwt({"alg": "HS256"}, {"name": "Zoë", "城": "市"})
        out = make_decode_jwt()({"token": token})
        # JSON dump uses ASCII escapes by default — that's still readable
        assert "Zo" in out


class TestBuilder:
    def test_builder(self):
        handlers = build_jwt_handlers()
        assert set(handlers.keys()) == {"decode_jwt"}
        assert callable(handlers["decode_jwt"])
