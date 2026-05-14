"""Tests for AgenticDebugger's pure-logic tool helpers.

These tests don't spin up a real Docker stack — they exercise the parts
of the new tooling that don't depend on `docker compose exec`. The
actual exec/curl/psql plumbing is covered by integration tests at the
runner level (and by manual M1 runs).
"""
import base64
import json
from unittest.mock import MagicMock

import pytest

from bizniz.agents.debugger.agentic import AgenticDebugger


def _make_debugger():
    """Build a minimally-wired AgenticDebugger for unit tests."""
    client = MagicMock()
    workspace = MagicMock()
    environment = MagicMock()
    return AgenticDebugger(
        client=client,
        workspace=workspace,
        environment=environment,
        compose_path="/tmp/fake/docker-compose.yml",
        service_name="backend",
    )


def _make_jwt(header: dict, payload: dict) -> str:
    """Build a fake JWT (signature is bogus but format is valid)."""
    def b64(d: dict) -> str:
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{b64(header)}.{b64(payload)}.fake-signature"


class TestDecodeJWT:
    def test_decodes_valid_jwt(self):
        debugger = _make_debugger()
        token = _make_jwt(
            {"alg": "RS256", "kid": "abc"},
            {"iss": "acme.com", "aud": "app-id", "roles": ["landlord"], "sub": "user-1"},
        )
        out = debugger._tool_decode_jwt(token)
        assert "Header" in out
        assert "Payload" in out
        assert "acme.com" in out
        assert "landlord" in out
        assert '"alg": "RS256"' in out

    def test_strips_bearer_prefix(self):
        debugger = _make_debugger()
        token = _make_jwt({"alg": "RS256"}, {"iss": "x"})
        out = debugger._tool_decode_jwt(f"Bearer {token}")
        assert '"iss": "x"' in out

    def test_rejects_empty(self):
        debugger = _make_debugger()
        assert "non-empty" in debugger._tool_decode_jwt("")
        assert "non-empty" in debugger._tool_decode_jwt("   ")

    def test_rejects_malformed(self):
        debugger = _make_debugger()
        out = debugger._tool_decode_jwt("not.ajwt")
        assert "expected 3 parts" in out

    def test_handles_invalid_base64(self):
        debugger = _make_debugger()
        out = debugger._tool_decode_jwt("xxx.yyy.zzz")
        assert "ERROR" in out


class TestResolveService:
    def test_returns_explicit_service(self):
        debugger = _make_debugger()
        assert debugger._resolve_service("auth") == "auth"

    def test_falls_back_to_bound_service(self):
        debugger = _make_debugger()
        assert debugger._resolve_service("") == "backend"

    def test_returns_none_when_neither(self):
        debugger = _make_debugger()
        debugger._service_name = None
        assert debugger._resolve_service("") is None


class TestShellQuote:
    def test_simple_string(self):
        assert AgenticDebugger._shell_quote("SELECT 1") == "'SELECT 1'"

    def test_escapes_single_quotes(self):
        # SQL with embedded apostrophe: SELECT 'a''b'
        out = AgenticDebugger._shell_quote("SELECT 'x'")
        # Should produce a string that is safe to embed in single-quotes
        assert "'\\''" in out


class TestGuessDBService:
    def test_finds_postgres_service(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  database:\n"
            "    image: postgres:16-alpine\n"
            "  backend:\n"
            "    image: my-backend:dev\n"
        )
        debugger = _make_debugger()
        debugger._compose_path = str(compose)
        assert debugger._guess_db_service() == "database"

    def test_returns_none_when_no_postgres(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  backend:\n"
            "    image: my-backend:dev\n"
        )
        debugger = _make_debugger()
        debugger._compose_path = str(compose)
        assert debugger._guess_db_service() is None

    def test_returns_none_on_unreadable_compose(self, tmp_path):
        debugger = _make_debugger()
        debugger._compose_path = str(tmp_path / "nonexistent.yml")
        assert debugger._guess_db_service() is None


class TestHitEndpointParsing:
    """Verify the request_data JSON parsing path. Doesn't actually hit
    a real container — just confirms the parser handles common shapes."""

    def test_rejects_empty_url(self):
        debugger = _make_debugger()
        out = debugger._tool_hit_endpoint("backend", "", "{}")
        assert "ERROR" in out and "url" in out

    def test_rejects_invalid_json(self):
        debugger = _make_debugger()
        # Real container call won't happen because we'll fail on parse first.
        out = debugger._tool_hit_endpoint(
            "backend",
            "http://backend:8000/x",
            "{this is not json",
        )
        assert "ERROR" in out and "request_data" in out
