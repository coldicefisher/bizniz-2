"""Integration tests for the logout proxy.

Confirms POST /api/v1/auth/logout is idempotent, best-effort, and
does not require a valid access JWT.
"""
from __future__ import annotations

import httpx

BACKEND_URL = "http://backend:8000"
LOGOUT_URL = f"{BACKEND_URL}/api/v1/auth/logout"


def test_logout_with_valid_refresh_token_returns_200() -> None:
    raise NotImplementedError("issue BE-005")


def test_logout_with_no_token_returns_200_idempotent() -> None:
    """Logout when already logged out returns 200."""
    raise NotImplementedError("issue BE-005")


def test_logout_with_malformed_refresh_token_returns_200() -> None:
    """Malformed refresh_token must not leak — still 200."""
    raise NotImplementedError("issue BE-005")


def test_logout_does_not_require_valid_access_jwt() -> None:
    """Logout works even with no Authorization header."""
    raise NotImplementedError("issue BE-005")
