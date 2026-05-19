"""Integration tests for the refresh-session proxy.

Confirms POST /api/v1/auth/refresh exchanges a refresh_token for a
fresh access_token via FusionAuth.
"""
from __future__ import annotations

import httpx

AUTH_URL = "http://auth:9011"
BACKEND_URL = "http://backend:8000"
APP_ID = "85a03867-dccf-4882-adde-1a79aeec50df"
LOGIN_URL = f"{BACKEND_URL}/api/v1/auth/login"
REFRESH_URL = f"{BACKEND_URL}/api/v1/auth/refresh"


def _login_and_get_refresh_token() -> str:
    """Helper: log in user@example.com and return the issued refresh_token."""
    raise NotImplementedError("issue BE-006")


def test_refresh_with_valid_token_returns_new_access_token() -> None:
    """Valid refresh → new access_token that decodes against JWKS with future exp."""
    raise NotImplementedError("issue BE-006")


def test_refresh_missing_token_returns_401() -> None:
    raise NotImplementedError("issue BE-006")


def test_refresh_invalid_token_returns_401_refresh_failed() -> None:
    """Garbage refresh_token → 401 with error='refresh_failed'."""
    raise NotImplementedError("issue BE-006")
