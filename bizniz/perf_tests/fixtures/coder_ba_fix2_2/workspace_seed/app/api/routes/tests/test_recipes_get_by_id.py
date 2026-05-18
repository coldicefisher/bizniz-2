"""Unit tests for GET /api/v1/recipes/{recipe_id} (BE-006-U4).

Covers the spec scenarios from the ``get_recipe`` capability:

1. ``happy_path`` — owner gets their recipe → 200 with full RecipeOut
   payload.
2. ``not_found_404`` — random UUID with no matching row → 404
   ``recipe_not_found``.
3. ``cross_user_returns_404`` — user B tries to GET user A's recipe →
   404 (NOT 403); response body identical to the absent-row case
   (no existence leak).
4. ``unauthenticated_401`` — no Authorization header → 401.
5. ``malformed_uuid_400_or_422`` — non-UUID path segment → 422 (raw
   FastAPI default; BE-006-U7 collapses to 400 later).
6. ``admin_no_cross_user_read`` — admin requesting a user's recipe is
   scoped to admin's own owner_id (404 if the admin doesn't own it).

Strategy mirrors ``test_recipes_delete.py`` and ``test_recipes_mine.py``:
mount the ``/recipes`` router on a bare FastAPI app, override
``get_current_user`` / ``get_db`` via ``app.dependency_overrides``, and
monkeypatch the repository's ``get_recipe_for_owner`` so the handler
can be exercised without a real Postgres connection.

The GET-by-id handler is async end-to-end (no ``run_sync`` bridge —
``get_recipe_for_owner`` is itself async), so the override session can
be any object the route passes straight through to the patched repo
stub.
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
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes import recipes
from app.api.routes.recipes import router
from app.core.auth import CurrentUser, get_current_user
from app.db.session import get_db


# ── Fixed test identities ────────────────────────────────

USER_A_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_A_EMAIL = "userA@example.com"
USER_A_DISPLAY_NAME = "Alice"

USER_B_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_B_EMAIL = "userB@example.com"
USER_B_DISPLAY_NAME = "Bob"

ADMIN_UUID = uuid.UUID("33333333-3333-3333-3333-333333333333")
ADMIN_EMAIL = "admin@example.com"

RECIPE_UUID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _current_user(
    *,
    user_id: uuid.UUID = USER_A_UUID,
    email: str = USER_A_EMAIL,
    display_name: str | None = USER_A_DISPLAY_NAME,
    role: str = "user",
) -> CurrentUser:
    """Build a ``CurrentUser`` for dependency-override injection."""
    return CurrentUser(
        id=user_id,
        email=email,
        display_name=display_name,
        role=role,
    )


def _recipe_row(
    *,
    recipe_id: uuid.UUID = RECIPE_UUID,
    owner_id: uuid.UUID = USER_A_UUID,
    title: str = "Pancakes",
    description: str = "Fluffy weekend pancakes.",
    ingredients: list[str] | None = None,
    instructions: list[str] | None = None,
    prep_time: int = 5,
    cook_time: int = 10,
    servings: int = 4,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> SimpleNamespace:
    """Build a Recipe-shaped row for ``response_model=RecipeOut`` projection.

    A ``SimpleNamespace`` is enough — Pydantic v2's
    ``from_attributes=True`` reads attributes off any object and does
    not require the real SQLAlchemy mapping.
    """
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=recipe_id,
        owner_id=owner_id,
        title=title,
        description=description,
        ingredients=ingredients or ["flour", "milk", "egg"],
        instructions=instructions or ["Mix.", "Cook on griddle."],
        prep_time=prep_time,
        cook_time=cook_time,
        servings=servings,
        created_at=created_at or now,
        updated_at=updated_at or now,
    )


def _make_app(*, current_user: CurrentUser | None) -> FastAPI:
    """Mount the /recipes router with auth/db dependency overrides.

    When ``current_user`` is None the real ``get_current_user`` runs —
    used by the no-auth case to assert that missing Authorization
    headers produce 401 from the real JWT-validation pipeline.
    """
    app = FastAPI()
    app.include_router(router)

    async def _override_db():
        yield SimpleNamespace(__sentinel__="db_session")

    app.dependency_overrides[get_db] = _override_db

    if current_user is not None:
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


def _patch_get_repo(
    monkeypatch,
    *,
    result=None,
    capture: dict | None = None,
):
    """Monkeypatch ``recipes.get_recipe_for_owner`` to a recording stub.

    Returns ``None`` by default (the 404 path); pass ``result`` to
    substitute a specific row. When ``capture`` is provided, each call
    records the ``recipe_id`` and ``owner_id`` it received — used by
    the owner-scoping tests to assert the route forwards the
    authenticated identity (not a client-supplied value).
    """

    async def _fake_get(session, *, recipe_id, owner_id):
        if capture is not None:
            capture["recipe_id"] = recipe_id
            capture["owner_id"] = owner_id
            capture["session"] = session
        return result

    monkeypatch.setattr(recipes, "get_recipe_for_owner", _fake_get)
    return _fake_get


# ── Router scaffold ──────────────────────────────────────


class TestRouterScaffold:
    """Module-level invariants the skeleton auto-mount depends on."""

    def test_router_is_apirouter(self):
        """``router`` must be a FastAPI ``APIRouter`` for auto-mount."""
        assert isinstance(router, APIRouter)

    def test_router_prefix_is_recipes(self):
        """Auto-mount adds /api/v1 — router declares only /recipes."""
        assert router.prefix == "/recipes"

    def test_get_by_id_route_registered_as_get(self):
        """GET /recipes/{recipe_id} must be registered with status 200."""
        routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/recipes/{recipe_id}"
            and "GET" in (getattr(r, "methods", set()) or set())
        ]
        assert routes, "GET /recipes/{recipe_id} not registered on the router"
        assert any(getattr(r, "status_code", None) == 200 for r in routes)

    def test_get_by_id_route_uses_get_current_user_dependency(self):
        """Auth gate: route must depend on ``get_current_user``.

        Without this guard a future refactor could accidentally drop
        ``Depends(require_roles(...))`` and the no_auth_401 case below
        would still pass against a stubbed app — this scaffold-level
        assertion catches the regression at import time.
        """
        get_routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/recipes/{recipe_id}"
            and "GET" in (getattr(r, "methods", set()) or set())
        ]
        assert get_routes
        route = get_routes[0]
        seen: list = []
        stack = [route.dependant]
        while stack:
            d = stack.pop()
            seen.append(d.call)
            stack.extend(d.dependencies)
        assert get_current_user in seen, (
            "GET /recipes/{recipe_id} must depend on get_current_user "
            "via require_roles"
        )

    def test_get_by_id_registered_after_mine(self):
        """Path-matching order: /mine MUST be registered before /{recipe_id}.

        FastAPI matches routes in registration order. If /{recipe_id}
        is declared first, /mine would parse as recipe_id='mine' and
        422 on UUID coercion — silently breaking the list endpoint.
        Locks the registration order so a future refactor can't
        accidentally shadow /mine.
        """
        paths = [getattr(r, "path", "") for r in router.routes]
        mine_idx = paths.index("/recipes/mine")
        # Find the FIRST occurrence of the parameterised path with GET.
        get_param_idx = None
        for i, r in enumerate(router.routes):
            if (
                getattr(r, "path", "") == "/recipes/{recipe_id}"
                and "GET" in (getattr(r, "methods", set()) or set())
            ):
                get_param_idx = i
                break
        assert get_param_idx is not None
        assert mine_idx < get_param_idx, (
            "/recipes/mine must be registered BEFORE GET /recipes/{recipe_id}"
        )


# ── Happy-path ───────────────────────────────────────────


class TestGetRecipeHappyPath:
    """Happy-path coverage of the get_recipe capability."""

    async def test_happy_path_returns_200_with_recipe_out(
        self, make_client, monkeypatch
    ):
        """Owner GETs their recipe → 200 with full RecipeOut body."""
        cu = _current_user()
        row = _recipe_row()
        captured: dict = {}
        _patch_get_repo(monkeypatch, result=row, capture=captured)

        app = _make_app(current_user=cu)
        client = await make_client(app)
        resp = await client.get(f"/recipes/{RECIPE_UUID}")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Full RecipeOut projection — every output-contract field present.
        assert {
            "id", "owner_id", "title", "description", "ingredients",
            "instructions", "prep_time", "cook_time", "servings",
            "created_at", "updated_at",
        }.issubset(body.keys())
        assert body["id"] == str(RECIPE_UUID)
        assert body["owner_id"] == str(USER_A_UUID)
        assert body["title"] == "Pancakes"
        assert body["ingredients"] == ["flour", "milk", "egg"]
        assert body["prep_time"] == 5
        # Repository helper was called with the right scoping args.
        assert captured["recipe_id"] == RECIPE_UUID
        assert captured["owner_id"] == USER_A_UUID

    async def test_admin_role_accepted_for_their_own_recipe(
        self, make_client, monkeypatch
    ):
        """Admin role is in ``require_roles(['user','admin'])`` → 200.

        Locks that admin can read recipes they own. Cross-user admin
        reads are explicitly out-of-scope this milestone (see
        ``admin_no_cross_user_read`` below).
        """
        cu = _current_user(user_id=ADMIN_UUID, email=ADMIN_EMAIL, role="admin")
        row = _recipe_row(owner_id=ADMIN_UUID)
        captured: dict = {}
        _patch_get_repo(monkeypatch, result=row, capture=captured)

        app = _make_app(current_user=cu)
        client = await make_client(app)
        resp = await client.get(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 200, resp.text
        assert captured["owner_id"] == ADMIN_UUID

    async def test_handler_passes_jwt_owner_id_to_repo(
        self, make_client, monkeypatch
    ):
        """Owner-scoping: ``owner_id`` forwarded to repo == JWT identity.

        Captures the kwargs the route passes to ``get_recipe_for_owner``
        and asserts ``owner_id`` equals the dependency-override's
        ``user.id`` — proving owner is sourced strictly from the JWT.
        """
        cu = _current_user()
        captured: dict = {}
        _patch_get_repo(monkeypatch, result=_recipe_row(), capture=captured)

        app = _make_app(current_user=cu)
        client = await make_client(app)
        resp = await client.get(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 200
        assert captured["owner_id"] == USER_A_UUID
        assert captured["recipe_id"] == RECIPE_UUID


# ── 404 cases (absent / wrong owner — collapsed) ─────────


class TestGetRecipeNotFound:
    """404 cases: absent row OR wrong owner — both surface identically."""

    async def test_random_uuid_returns_404(self, make_client, monkeypatch):
        """Random recipe id with no matching row → 404 recipe_not_found."""
        cu = _current_user()
        _patch_get_repo(monkeypatch, result=None)

        app = _make_app(current_user=cu)
        client = await make_client(app)
        resp = await client.get(f"/recipes/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "recipe_not_found"}

    async def test_cross_user_returns_404_not_403(
        self, make_client, monkeypatch
    ):
        """User B requesting user A's recipe → 404 (NOT 403).

        Locks the existence-leak invariant: response body for a
        wrong-owner attempt must be identical to a true 404 — a user
        must not be able to distinguish "absent" from "owned by
        someone else" by comparing error codes or bodies.
        """
        cu_b = _current_user(
            user_id=USER_B_UUID,
            email=USER_B_EMAIL,
            display_name=USER_B_DISPLAY_NAME,
        )
        captured: dict = {}
        # The repo's WHERE id=:recipe_id AND owner_id=:owner_id returns
        # None when the row is owned by someone else (rowcount=0).
        _patch_get_repo(monkeypatch, result=None, capture=captured)

        app = _make_app(current_user=cu_b)
        client = await make_client(app)
        # RECIPE_UUID is user A's recipe per the test fixture; user B
        # asks — repo sees owner_id=USER_B_UUID and returns None.
        resp = await client.get(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 404
        # Body identical to absent-row case — no existence leak.
        assert resp.json() == {"detail": "recipe_not_found"}
        # Owner scope passed to repo was the requester (user B),
        # never the row's true owner (user A).
        assert captured["owner_id"] == USER_B_UUID

    async def test_admin_no_cross_user_read(self, make_client, monkeypatch):
        """Admin requesting another user's recipe → 404 (no cross-user read).

        Admin moderation ships later; in M2 the admin role is scoped
        to their own ``owner_id`` exactly like a regular user. The
        repo's combined WHERE returns None when the row exists but is
        owned by someone else, and the handler must surface 404 —
        admins are NOT a special case in this milestone.
        """
        cu_admin = _current_user(
            user_id=ADMIN_UUID, email=ADMIN_EMAIL, role="admin",
        )
        captured: dict = {}
        _patch_get_repo(monkeypatch, result=None, capture=captured)

        app = _make_app(current_user=cu_admin)
        client = await make_client(app)
        resp = await client.get(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "recipe_not_found"}
        # Owner scope passed was admin's id — NOT bypassed to fetch
        # cross-user rows.
        assert captured["owner_id"] == ADMIN_UUID


# ── Auth / role gating ───────────────────────────────────


class TestGetRecipeAuth:
    """401 / 403 cases from the require_roles dependency."""

    async def test_unauthenticated_returns_401(self, make_client):
        """No Authorization header → 401 from the real dependency."""
        app = _make_app(current_user=None)
        client = await make_client(app)

        resp = await client.get(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 401

    async def test_unauthenticated_does_not_call_repository(
        self, make_client, monkeypatch
    ):
        """A 401 short-circuit must never reach the DB layer."""
        called: list[str] = []

        async def _boom_get(session, *, recipe_id, owner_id):
            called.append("get_recipe_for_owner")
            raise AssertionError(
                "get_recipe_for_owner must not run on unauthenticated GET"
            )

        monkeypatch.setattr(recipes, "get_recipe_for_owner", _boom_get)

        app = _make_app(current_user=None)
        client = await make_client(app)
        resp = await client.get(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 401
        assert called == []

    async def test_super_admin_role_is_forbidden(
        self, make_client, monkeypatch
    ):
        """``super_admin`` is NOT in require_roles(['user','admin']) → 403."""
        cu = _current_user(role="super_admin")
        called: list[str] = []

        async def _boom_get(session, *, recipe_id, owner_id):
            called.append("get_recipe_for_owner")
            raise AssertionError("must not run for super_admin")

        monkeypatch.setattr(recipes, "get_recipe_for_owner", _boom_get)

        app = _make_app(current_user=cu)
        client = await make_client(app)
        resp = await client.get(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 403
        assert called == []


# ── No mirror self-heal on GET-by-id ─────────────────────


class TestGetRecipeNoMirrorSelfHeal:
    """GET-by-id must NOT call ensure_local_user — read-only, no FK touch."""

    async def test_route_does_not_call_ensure_local_user(
        self, make_client, monkeypatch
    ):
        """GET-by-id must not invoke the mirror-upsert helper.

        The contract says: read endpoints are read-only and have no
        FK dependency on ``users``. The POST handler is where the
        self-heal lives. Patch ``recipes.ensure_local_user`` to
        explode; if the handler accidentally calls it the test fails
        loud.
        """
        cu = _current_user()
        called: list[str] = []

        async def _boom(db, *, jwt_claims):
            called.append("ensure_local_user")
            raise AssertionError(
                "GET /recipes/{recipe_id} must not call ensure_local_user"
            )

        monkeypatch.setattr(recipes, "ensure_local_user", _boom)
        _patch_get_repo(monkeypatch, result=_recipe_row())

        app = _make_app(current_user=cu)
        client = await make_client(app)
        resp = await client.get(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 200
        assert called == []


# ── Malformed path UUID ──────────────────────────────────


class TestGetRecipeMalformedUUID:
    """Non-UUID path segment → 422 (raw FastAPI); U7 collapses to 400."""

    async def test_malformed_uuid_rejected(self, make_client, monkeypatch):
        """A non-UUID recipe_id must be rejected before the repo runs."""
        cu = _current_user()
        called: list[str] = []

        async def _boom_get(session, *, recipe_id, owner_id):
            called.append("get_recipe_for_owner")
            raise AssertionError(
                "get_recipe_for_owner must not run on malformed UUID"
            )

        monkeypatch.setattr(recipes, "get_recipe_for_owner", _boom_get)

        app = _make_app(current_user=cu)
        client = await make_client(app)
        resp = await client.get("/recipes/not-a-uuid")
        # BE-006-U7 collapses 422 → 400. Accept either here.
        assert resp.status_code in (400, 422)
        assert called == []
