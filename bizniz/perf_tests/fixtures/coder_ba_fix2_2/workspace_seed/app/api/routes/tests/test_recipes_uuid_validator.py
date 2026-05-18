"""Unit tests for the BE-006-U7 422→400 UUID-coercion dependency.

The ``get_recipe`` / ``update_recipe`` / ``delete_recipe`` capability
contracts each explicitly list ``400 — recipe_id is not a valid UUID``.
FastAPI's default behavior — declaring ``recipe_id: UUID`` directly on
the handler — surfaces a malformed segment as 422 via the
``RequestValidationError`` handler, which violates the contract.

BE-006-U7 introduces ``_validate_recipe_id`` — a small dependency that
parses the path segment, raises ``HTTPException(400)`` with detail
``invalid_recipe_id`` on malformed input, and returns the parsed
:class:`UUID` to the handler on success.

Coverage:

1. ``test_dependency_returns_uuid_on_valid_input`` — the dependency
   itself returns a real ``UUID`` object when given a well-formed
   string (the happy path; locks the contract that callers can rely on
   the return type).
2. ``test_dependency_raises_400_on_malformed_input`` — direct call
   with garbage raises ``HTTPException(status_code=400,
   detail='invalid_recipe_id')`` (locks the error envelope shape).
3. ``test_get_malformed_uuid_returns_400`` — GET /recipes/<garbage>
   → 400 with ``invalid_recipe_id`` detail (was 422 pre-U7).
4. ``test_put_malformed_uuid_returns_400`` — PUT /recipes/<garbage>
   → 400 with ``invalid_recipe_id`` detail. PUT carries a body too;
   the test sends a valid body so we are sure the 400 came from the
   path-param dependency and NOT from body-validation.
5. ``test_delete_malformed_uuid_returns_400`` — DELETE /recipes/<garbage>
   → 400 with ``invalid_recipe_id`` detail.
6. ``test_repo_not_invoked_on_malformed_uuid`` — the matching
   repository function MUST NOT be called when the dependency rejects;
   verifies the dependency runs BEFORE the handler body, not after.
7. ``test_valid_uuid_passes_through`` — a well-formed UUID is forwarded
   to the repo as a real ``uuid.UUID`` instance (proves the dependency
   doesn't accidentally pass through a string).
8. ``test_uppercase_uuid_accepted`` — UUIDs are case-insensitive
   per the capability spec edge cases; the dependency must accept
   uppercase variants.
9. ``test_get_uses_validate_recipe_id_dependency`` — scaffold-level
   guard: the dependant tree for GET ``/{recipe_id}`` must include
   ``_validate_recipe_id``. Without this a future refactor could
   accidentally drop the dependency and the malformed cases above
   would still pass against a regressed app that emitted 422 with
   the wrong tests, but here we pin the wiring at import time.
10. ``test_put_uses_validate_recipe_id_dependency`` — same guard,
    PUT route.
11. ``test_delete_uses_validate_recipe_id_dependency`` — same guard,
    DELETE route.

Strategy mirrors the sibling unit tests
(test_recipes_get_by_id.py, test_recipes_put.py, test_recipes_delete.py):
mount the ``/recipes`` router on a bare FastAPI app, override
``get_current_user`` / ``get_db`` via ``app.dependency_overrides``, and
monkeypatch each repository function so the handler can be exercised
without a real Postgres connection.
"""
# Settings() requires FUSIONAUTH_TENANT_ID / _APPLICATION_ID / _API_KEY
# at import time. Fill in safe defaults before app.core.config is
# imported transitively by anything below.
import os as _os

_os.environ.setdefault(
    "FUSIONAUTH_TENANT_ID", "d4465dd9-12e7-4715-bc4e-690874974b6b"
)
_os.environ.setdefault(
    "FUSIONAUTH_APPLICATION_ID", "85a03867-dccf-4882-adde-1a79aeec50df"
)
_os.environ.setdefault("FUSIONAUTH_API_KEY", "test-api-key-xyz")

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.routes import recipes
from app.api.routes.recipes import _validate_recipe_id, router
from app.core.auth import CurrentUser, get_current_user
from app.db.session import get_db


# ── Fixed test identities ────────────────────────────────

USER_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_EMAIL = "user@example.com"

VALID_RECIPE_UUID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _current_user(
    *,
    user_id: uuid.UUID = USER_UUID,
    email: str = USER_EMAIL,
    role: str = "user",
) -> CurrentUser:
    """Build a ``CurrentUser`` for dependency-override injection."""
    return CurrentUser(
        id=user_id,
        email=email,
        display_name="Test User",
        role=role,
    )


def _recipe_row(
    *,
    recipe_id: uuid.UUID = VALID_RECIPE_UUID,
    owner_id: uuid.UUID = USER_UUID,
) -> SimpleNamespace:
    """Build a Recipe-shaped row for ``response_model=RecipeOut`` projection."""
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=recipe_id,
        owner_id=owner_id,
        title="Pancakes",
        description="Fluffy weekend pancakes.",
        ingredients=["flour", "milk", "egg"],
        instructions=["Mix.", "Cook on griddle."],
        prep_time=5,
        cook_time=10,
        servings=4,
        created_at=now,
        updated_at=now,
    )


def _valid_put_body() -> dict:
    """A RecipeCreate-shaped body that satisfies all field validation.

    Used by the PUT malformed-UUID test so the response is guaranteed
    to come from the path-param dependency (not from body validation).
    """
    return {
        "title": "Updated Pancakes",
        "description": "Updated description.",
        "ingredients": ["flour", "milk", "egg"],
        "instructions": ["Mix.", "Cook."],
        "prep_time": 5,
        "cook_time": 10,
        "servings": 4,
    }


def _make_app(*, current_user: CurrentUser) -> FastAPI:
    """Mount the /recipes router with auth/db dependency overrides."""
    app = FastAPI()
    app.include_router(router)

    async def _override_db():
        yield SimpleNamespace(__sentinel__="db_session")

    app.dependency_overrides[get_db] = _override_db

    async def _override_current_user() -> CurrentUser:
        return current_user

    app.dependency_overrides[get_current_user] = _override_current_user

    return app


@pytest.fixture
async def make_client():
    """Factory yielding ASGI clients bound to test apps."""
    clients: list[AsyncClient] = []

    async def _make(app: FastAPI) -> AsyncClient:
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        await client.__aenter__()
        clients.append(client)
        return client

    yield _make

    for c in clients:
        await c.__aexit__(None, None, None)


# ── Direct dependency-function tests ─────────────────────


class TestValidateRecipeIdDependency:
    """Unit-level coverage of ``_validate_recipe_id`` in isolation."""

    def test_dependency_returns_uuid_on_valid_input(self):
        """Well-formed UUID string → real ``uuid.UUID`` instance."""
        result = _validate_recipe_id(recipe_id=str(VALID_RECIPE_UUID))
        assert isinstance(result, uuid.UUID)
        assert result == VALID_RECIPE_UUID

    def test_dependency_raises_400_on_malformed_input(self):
        """Garbage path segment → ``HTTPException(400, 'invalid_recipe_id')``."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_recipe_id(recipe_id="not-a-uuid")
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "invalid_recipe_id"

    def test_dependency_raises_400_on_empty_string(self):
        """Empty path segment → 400 (UUID() rejects empty string)."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_recipe_id(recipe_id="")
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "invalid_recipe_id"

    def test_dependency_accepts_uppercase_uuid(self):
        """UUIDs are case-insensitive (capability edge case)."""
        upper = str(VALID_RECIPE_UUID).upper()
        result = _validate_recipe_id(recipe_id=upper)
        assert isinstance(result, uuid.UUID)
        assert result == VALID_RECIPE_UUID


# ── Route-level integration: GET ─────────────────────────


class TestGetMalformedUUID:
    """GET /recipes/{recipe_id}: malformed segment → 400 invalid_recipe_id."""

    async def test_get_malformed_uuid_returns_400(self, make_client, monkeypatch):
        """Non-UUID path → 400, body detail == 'invalid_recipe_id'."""
        called: list[str] = []

        async def _boom_get(session, *, recipe_id, owner_id):
            called.append("get_recipe_for_owner")
            raise AssertionError(
                "get_recipe_for_owner must not run on malformed UUID"
            )

        monkeypatch.setattr(recipes, "get_recipe_for_owner", _boom_get)

        app = _make_app(current_user=_current_user())
        client = await make_client(app)
        resp = await client.get("/recipes/not-a-uuid")

        assert resp.status_code == 400, resp.text
        assert resp.json() == {"detail": "invalid_recipe_id"}
        assert called == [], (
            "Repo MUST NOT be invoked when the UUID dependency rejects"
        )

    async def test_get_valid_uuid_forwards_real_uuid_to_repo(
        self, make_client, monkeypatch
    ):
        """Well-formed UUID → repo receives a real ``uuid.UUID`` instance."""
        captured: dict = {}

        async def _fake_get(session, *, recipe_id, owner_id):
            captured["recipe_id"] = recipe_id
            captured["owner_id"] = owner_id
            return _recipe_row()

        monkeypatch.setattr(recipes, "get_recipe_for_owner", _fake_get)

        app = _make_app(current_user=_current_user())
        client = await make_client(app)
        resp = await client.get(f"/recipes/{VALID_RECIPE_UUID}")

        assert resp.status_code == 200, resp.text
        assert isinstance(captured["recipe_id"], uuid.UUID), (
            "Handler must receive a parsed uuid.UUID, not a raw string"
        )
        assert captured["recipe_id"] == VALID_RECIPE_UUID

    async def test_get_uppercase_uuid_accepted(self, make_client, monkeypatch):
        """UUID case-insensitivity preserved through the dependency."""
        captured: dict = {}

        async def _fake_get(session, *, recipe_id, owner_id):
            captured["recipe_id"] = recipe_id
            return _recipe_row()

        monkeypatch.setattr(recipes, "get_recipe_for_owner", _fake_get)

        app = _make_app(current_user=_current_user())
        client = await make_client(app)
        upper = str(VALID_RECIPE_UUID).upper()
        resp = await client.get(f"/recipes/{upper}")

        assert resp.status_code == 200, resp.text
        assert captured["recipe_id"] == VALID_RECIPE_UUID


# ── Route-level integration: PUT ─────────────────────────


class TestPutMalformedUUID:
    """PUT /recipes/{recipe_id}: malformed segment → 400 invalid_recipe_id."""

    async def test_put_malformed_uuid_returns_400(self, make_client, monkeypatch):
        """Non-UUID path + valid body → 400 from the dependency, not 422."""
        called: list[str] = []

        def _boom_update(session, *, recipe_id, owner_id, data):
            called.append("_update_recipe_for_owner")
            raise AssertionError(
                "_update_recipe_for_owner must not run on malformed UUID"
            )

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _boom_update)

        app = _make_app(current_user=_current_user())
        client = await make_client(app)
        # Valid body so the 400 cannot come from RecipeCreate validation;
        # any failure here is the path-param dependency.
        resp = await client.put("/recipes/not-a-uuid", json=_valid_put_body())

        assert resp.status_code == 400, resp.text
        assert resp.json() == {"detail": "invalid_recipe_id"}
        assert called == [], (
            "Update helper MUST NOT be invoked when the UUID dependency rejects"
        )


# ── Route-level integration: DELETE ──────────────────────


class TestDeleteMalformedUUID:
    """DELETE /recipes/{recipe_id}: malformed segment → 400 invalid_recipe_id."""

    async def test_delete_malformed_uuid_returns_400(
        self, make_client, monkeypatch
    ):
        """Non-UUID path → 400, body detail == 'invalid_recipe_id'."""
        called: list[str] = []

        def _boom_delete(session, *, recipe_id, owner_id):
            called.append("delete_recipe_for_owner")
            raise AssertionError(
                "delete_recipe_for_owner must not run on malformed UUID"
            )

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _boom_delete)

        app = _make_app(current_user=_current_user())
        client = await make_client(app)
        resp = await client.delete("/recipes/not-a-uuid")

        assert resp.status_code == 400, resp.text
        assert resp.json() == {"detail": "invalid_recipe_id"}
        assert called == [], (
            "Delete repo MUST NOT be invoked when the UUID dependency rejects"
        )


# ── Scaffold-level wiring guards ─────────────────────────


class TestWiringGuards:
    """Lock the dependency wiring at import time so a refactor can't regress it.

    Without these, a future change could drop ``Depends(_validate_recipe_id)``
    from any of GET/PUT/DELETE — the malformed-UUID tests above would
    then fail loudly, but only at run time. These guards walk the
    dependant tree and assert ``_validate_recipe_id`` is present.
    """

    def _routes_for(self, *, path: str, method: str) -> list:
        return [
            r for r in router.routes
            if getattr(r, "path", "") == path
            and method in (getattr(r, "methods", set()) or set())
        ]

    def _depends_on_validator(self, route) -> bool:
        seen: list = []
        stack = [route.dependant]
        while stack:
            d = stack.pop()
            seen.append(d.call)
            stack.extend(d.dependencies)
        return _validate_recipe_id in seen

    def test_get_uses_validate_recipe_id_dependency(self):
        """GET ``/{recipe_id}`` must depend on ``_validate_recipe_id``."""
        routes = self._routes_for(path="/recipes/{recipe_id}", method="GET")
        assert routes, "GET /recipes/{recipe_id} not registered"
        assert self._depends_on_validator(routes[0]), (
            "GET /recipes/{recipe_id} must use _validate_recipe_id "
            "for the 422→400 UUID coercion"
        )

    def test_put_uses_validate_recipe_id_dependency(self):
        """PUT ``/{recipe_id}`` must depend on ``_validate_recipe_id``."""
        routes = self._routes_for(path="/recipes/{recipe_id}", method="PUT")
        assert routes, "PUT /recipes/{recipe_id} not registered"
        assert self._depends_on_validator(routes[0]), (
            "PUT /recipes/{recipe_id} must use _validate_recipe_id "
            "for the 422→400 UUID coercion"
        )

    def test_delete_uses_validate_recipe_id_dependency(self):
        """DELETE ``/{recipe_id}`` must depend on ``_validate_recipe_id``."""
        routes = self._routes_for(path="/recipes/{recipe_id}", method="DELETE")
        assert routes, "DELETE /recipes/{recipe_id} not registered"
        assert self._depends_on_validator(routes[0]), (
            "DELETE /recipes/{recipe_id} must use _validate_recipe_id "
            "for the 422→400 UUID coercion"
        )
