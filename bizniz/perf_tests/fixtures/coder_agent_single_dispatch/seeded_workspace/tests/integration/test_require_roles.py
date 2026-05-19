"""Tests for the require_roles dependency from app.core.auth.

No production route in this milestone uses require_roles, but the
helper must be verified — see require_role_authorization in the spec.
Exercise it directly with constructed User objects, or via
FastAPI dependency_overrides on a tiny test-only route registered
inside the test (do NOT add production routes).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.auth import require_roles
from app.models.user import User


def test_require_roles_allows_user_with_matching_role() -> None:
    raise NotImplementedError("issue BE-007")


def test_require_roles_rejects_user_without_role_403() -> None:
    raise NotImplementedError("issue BE-007")


def test_require_roles_super_admin_satisfies_admin_check() -> None:
    """super_admin role implicitly satisfies an 'admin' requirement."""
    raise NotImplementedError("issue BE-007")


def test_require_roles_admin_does_not_imply_user_role() -> None:
    """An admin-only account must NOT silently satisfy a 'user' requirement."""
    raise NotImplementedError("issue BE-007")


def test_require_roles_missing_current_user_returns_401() -> None:
    """Defensive: helper reached without an authenticated user → 401."""
    raise NotImplementedError("issue BE-007")
