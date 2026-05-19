"""Integration tests for GET /api/v1/me against the live stack."""
from __future__ import annotations

import httpx
import pytest

AUTH_URL = "http://auth:9011"
BACKEND_URL = "http://backend:8000"
APP_ID = "85a03867-dccf-4882-adde-1a79aeec50df"
ME_URL = f"{BACKEND_URL}/api/v1/me"


def _login(email: str, password: str) -> str:
    """Acquire a JWT for an existing FusionAuth user via /api/login."""
    raise NotImplementedError("issue BE-002")


@pytest.fixture
def user_token() -> str:
    """Bearer token for user@example.com."""
    raise NotImplementedError("issue BE-002")


@pytest.fixture
def admin_token() -> str:
    """Bearer token for admin@example.com."""
    raise NotImplementedError("issue BE-002")


def test_me_returns_identity_for_user(user_token: str) -> None:
    """GET /me with a valid user token returns id/email/full_name/roles."""
    raise NotImplementedError("issue BE-002")


def test_me_includes_admin_role_for_admin_user(admin_token: str) -> None:
    """GET /me for admin@example.com returns roles containing 'admin'."""
    raise NotImplementedError("issue BE-002")


def test_me_missing_authorization_header_returns_401() -> None:
    raise NotImplementedError("issue BE-002")


def test_me_non_bearer_scheme_returns_401() -> None:
    raise NotImplementedError("issue BE-002")


def test_me_malformed_jwt_returns_401() -> None:
    raise NotImplementedError("issue BE-002")


def test_me_alg_none_token_rejected_401() -> None:
    """Algorithm-confusion defense: alg=none must NOT be accepted."""
    raise NotImplementedError("issue BE-002")


def test_me_expired_token_returns_401() -> None:
    raise NotImplementedError("issue BE-002")
