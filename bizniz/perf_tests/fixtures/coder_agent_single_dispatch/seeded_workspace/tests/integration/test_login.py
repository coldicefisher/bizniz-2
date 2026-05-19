"""Integration tests for the login proxy.

Confirms the skeleton-shipped POST /api/v1/auth/login proxies to
FusionAuth /api/login and returns the unmodified JWT.
"""
from __future__ import annotations

import httpx

AUTH_URL = "http://auth:9011"
BACKEND_URL = "http://backend:8000"
APP_ID = "85a03867-dccf-4882-adde-1a79aeec50df"
LOGIN_URL = f"{BACKEND_URL}/api/v1/auth/login"


def test_login_with_valid_credentials_returns_access_token() -> None:
    """Valid login returns a JWT that decodes against JWKS with iss/aud matching the contract."""
    raise NotImplementedError("issue BE-004")


def test_login_missing_email_returns_400() -> None:
    raise NotImplementedError("issue BE-004")


def test_login_missing_password_returns_400() -> None:
    raise NotImplementedError("issue BE-004")


def test_login_wrong_password_returns_401_generic() -> None:
    """Wrong password → 401 with a generic body (no enumeration)."""
    raise NotImplementedError("issue BE-004")


def test_login_unknown_email_returns_401_generic() -> None:
    """Unknown email → 401 with the SAME generic body as wrong password."""
    raise NotImplementedError("issue BE-004")


def test_login_client_supplied_applicationid_is_ignored() -> None:
    """Server injects the primary application id; client-supplied junk is ignored."""
    raise NotImplementedError("issue BE-004")
