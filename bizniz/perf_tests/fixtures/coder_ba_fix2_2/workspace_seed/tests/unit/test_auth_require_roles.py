"""Unit tests for the ``require_roles`` dependency factory (BE-006-U6).

The factory builds an inner ``_checker`` that takes a pre-validated
:class:`CurrentUser` (from :func:`get_current_user`) and enforces a
role gate. These tests call ``_checker`` directly with a constructed
``CurrentUser`` — no FastAPI request lifecycle, no JWT, no DB — so
the gate's semantics are exercised in isolation.

Coverage:

* Varargs form: ``require_roles("admin")`` and
  ``require_roles("admin", "super_admin")``.
* Single-list form: ``require_roles(["admin"])`` — the call style
  named in the issue spec.
* Single-tuple form: ``require_roles(("admin",))`` — equivalent for
  callers that already have a tuple.
* Allowed role → returns the same ``CurrentUser`` unchanged.
* Disallowed role → ``HTTPException(403, detail={'error':
  'forbidden'})``.
* Empty allowed set → every role rejected with 403.
* Factory returns a callable each time (FastAPI ``Depends`` contract).
* Inner checker uses ``Depends(get_current_user)`` as its default
  parameter — the composition seam the issue spec mandates.
"""
from __future__ import annotations

import inspect
import os
import uuid

import pytest

# Mirror the env hygiene of the other BE-006 unit tests so that
# ``get_settings()`` (imported transitively via app.core.auth) doesn't
# fail in a stripped dev container.
os.environ.setdefault(
    "FUSIONAUTH_TENANT_ID", "d4465dd9-12e7-4715-bc4e-690874974b6b"
)
os.environ.setdefault(
    "FUSIONAUTH_APPLICATION_ID", "85a03867-dccf-4882-adde-1a79aeec50df"
)
os.environ.setdefault("FUSIONAUTH_API_KEY", "test-api-key-xyz")

from fastapi import HTTPException
from fastapi.params import Depends as DependsMarker

from app.core.auth import CurrentUser, get_current_user, require_roles


def _make_current_user(role: str) -> CurrentUser:
    return CurrentUser(
        id=uuid.uuid4(),
        email="user@example.com",
        display_name="Test User",
        role=role,
    )


class TestFactoryReturnsCallable:
    def test_varargs_returns_callable(self) -> None:
        checker = require_roles("admin")
        assert callable(checker)

    def test_list_form_returns_callable(self) -> None:
        checker = require_roles(["admin"])
        assert callable(checker)

    def test_tuple_form_returns_callable(self) -> None:
        checker = require_roles(("admin",))
        assert callable(checker)

    def test_each_call_returns_a_fresh_callable(self) -> None:
        # Two separate Depends(require_roles(...)) at different routes
        # must not share state.
        a = require_roles("admin")
        b = require_roles("admin")
        assert a is not b

    def test_inner_checker_is_async(self) -> None:
        checker = require_roles("admin")
        assert inspect.iscoroutinefunction(checker)


class TestInnerCheckerSignature:
    """The composition seam: ``Depends(get_current_user)`` default."""

    def test_current_user_param_default_is_depends_on_get_current_user(
        self,
    ) -> None:
        checker = require_roles("admin")
        sig = inspect.signature(checker)
        assert "current_user" in sig.parameters
        default = sig.parameters["current_user"].default
        assert isinstance(default, DependsMarker)
        assert default.dependency is get_current_user


class TestAllowsMatchingRole:
    @pytest.mark.asyncio
    async def test_varargs_single_role_match(self) -> None:
        checker = require_roles("admin")
        cu = _make_current_user("admin")
        assert await checker(current_user=cu) is cu

    @pytest.mark.asyncio
    async def test_varargs_multiple_roles_first_match(self) -> None:
        checker = require_roles("admin", "super_admin")
        cu = _make_current_user("admin")
        assert await checker(current_user=cu) is cu

    @pytest.mark.asyncio
    async def test_varargs_multiple_roles_second_match(self) -> None:
        checker = require_roles("admin", "super_admin")
        cu = _make_current_user("super_admin")
        assert await checker(current_user=cu) is cu

    @pytest.mark.asyncio
    async def test_list_form_allows_role_in_list(self) -> None:
        checker = require_roles(["admin", "super_admin"])
        cu = _make_current_user("super_admin")
        assert await checker(current_user=cu) is cu

    @pytest.mark.asyncio
    async def test_single_element_list_allows(self) -> None:
        checker = require_roles(["user"])
        cu = _make_current_user("user")
        assert await checker(current_user=cu) is cu

    @pytest.mark.asyncio
    async def test_tuple_form_allows_role(self) -> None:
        checker = require_roles(("admin", "user"))
        cu = _make_current_user("user")
        assert await checker(current_user=cu) is cu


class TestRejectsDisallowedRole:
    @pytest.mark.asyncio
    async def test_user_rejected_when_only_admin_allowed(self) -> None:
        checker = require_roles("admin")
        cu = _make_current_user("user")
        with pytest.raises(HTTPException) as exc:
            await checker(current_user=cu)
        assert exc.value.status_code == 403
        assert exc.value.detail == {"error": "forbidden"}

    @pytest.mark.asyncio
    async def test_admin_rejected_when_only_super_admin_allowed(self) -> None:
        checker = require_roles(["super_admin"])
        cu = _make_current_user("admin")
        with pytest.raises(HTTPException) as exc:
            await checker(current_user=cu)
        assert exc.value.status_code == 403
        assert exc.value.detail == {"error": "forbidden"}

    @pytest.mark.asyncio
    async def test_unknown_role_rejected(self) -> None:
        checker = require_roles("admin", "user")
        cu = _make_current_user("guest")
        with pytest.raises(HTTPException) as exc:
            await checker(current_user=cu)
        assert exc.value.status_code == 403
        assert exc.value.detail == {"error": "forbidden"}

    @pytest.mark.asyncio
    async def test_empty_list_rejects_every_role(self) -> None:
        checker = require_roles([])
        cu = _make_current_user("admin")
        with pytest.raises(HTTPException) as exc:
            await checker(current_user=cu)
        assert exc.value.status_code == 403
        assert exc.value.detail == {"error": "forbidden"}


class TestErrorShapeMatchesContract:
    """Auth error vocabulary stays consistent — detail is a dict."""

    @pytest.mark.asyncio
    async def test_detail_is_a_dict_not_a_string(self) -> None:
        checker = require_roles("admin")
        cu = _make_current_user("user")
        with pytest.raises(HTTPException) as exc:
            await checker(current_user=cu)
        assert isinstance(exc.value.detail, dict)
        assert exc.value.detail["error"] == "forbidden"

    @pytest.mark.asyncio
    async def test_status_code_is_403_forbidden(self) -> None:
        checker = require_roles("admin")
        cu = _make_current_user("user")
        with pytest.raises(HTTPException) as exc:
            await checker(current_user=cu)
        assert exc.value.status_code == 403
