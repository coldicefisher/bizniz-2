"""Integration tests for the signup proxy.

Confirms the skeleton-shipped POST /api/v1/auth/register correctly
proxies to FusionAuth's POST /api/user/registration (no path arg).
"""
from __future__ import annotations

import secrets

import httpx

AUTH_URL = "http://auth:9011"
BACKEND_URL = "http://backend:8000"
APP_ID = "85a03867-dccf-4882-adde-1a79aeec50df"
SIGNUP_URL = f"{BACKEND_URL}/api/v1/auth/register"
LOGIN_URL = f"{BACKEND_URL}/api/v1/auth/login"
ME_URL = f"{BACKEND_URL}/api/v1/me"


def _fresh_email() -> str:
    """Return a unique email so each test creates a new FusionAuth user."""
    raise NotImplementedError("issue BE-003")


def test_signup_creates_user_and_returns_id() -> None:
    raise NotImplementedError("issue BE-003")


def test_signup_missing_email_returns_400() -> None:
    raise NotImplementedError("issue BE-003")


def test_signup_missing_password_returns_400() -> None:
    raise NotImplementedError("issue BE-003")


def test_signup_malformed_email_returns_400() -> None:
    raise NotImplementedError("issue BE-003")


def test_signup_weak_password_returns_400_with_fielderrors() -> None:
    raise NotImplementedError("issue BE-003")


def test_signup_duplicate_email_returns_409() -> None:
    raise NotImplementedError("issue BE-003")


def test_signup_client_supplied_role_admin_is_ignored() -> None:
    """Client-supplied role='admin' must NOT escalate; the new account has only 'user'."""
    raise NotImplementedError("issue BE-003")
