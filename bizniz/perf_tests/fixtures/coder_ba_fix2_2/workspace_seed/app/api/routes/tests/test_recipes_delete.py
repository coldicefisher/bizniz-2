"""Unit tests for DELETE /api/v1/recipes/{recipe_id} (BE-006-U6).

Covers the spec scenarios from the ``delete_recipe`` capability:

1. ``happy_path`` — owner deletes → 204 with empty body.
2. ``cross_user_returns_404`` — user B tries to delete user A's
   recipe → 404 (NOT 403); response body identical to the absent-row
   case (no existence leak).
3. ``not_found_404`` — random UUID with no row → 404.
4. ``unauthenticated_401`` — no Authorization header → 401.
5. ``double_delete`` — first call 204, second call 404 (repo returns
   False on missing row → idempotency boundary).
6. ``malformed_uuid_400_or_422`` — non-UUID path segment → 422 (raw
   FastAPI default; BE-006-U7 collapses to 400 later).

Strategy mirrors ``test_recipes_put.py``: mount the ``/recipes``
router on a bare FastAPI app, override ``get_current_user`` /
``get_db`` via ``app.dependency_overrides``, and monkeypatch the
repository ``delete_recipe_for_owner`` so the handler can be
exercised without a real Postgres connection.

The DELETE handler calls ``delete_recipe_for_owner`` (sync) through
``db.run_sync``. The test session is a ``SimpleNamespace`` that
intercepts ``run_sync`` and invokes the lambda with a stub
sync-session — that way we observe exactly what the route does
instead of poking ORM internals.
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


class _FakeSession:
    """AsyncSession stand-in that records calls and forwards run_sync.

    The DELETE handler invokes
    ``await db.run_sync(lambda s: delete_recipe_for_owner(s, ...))``.
    We intercept the lambda by passing a dummy sync-session
    (``SimpleNamespace()``) and let the lambda run — but
    ``delete_recipe_for_owner`` is monkeypatched in each test, so
    the lambda never actually touches a real Session.
    """

    def __init__(self):
        self.run_sync_calls: list = []

    async def run_sync(self, fn):
        self.run_sync_calls.append(fn)
        return fn(SimpleNamespace())


def _make_app(
    *,
    current_user: CurrentUser | None,
) -> tuple[FastAPI, _FakeSession]:
    """Mount the /recipes router with auth/db overrides.

    Returns ``(app, fake_session)`` so the test can introspect the
    session afterwards. When ``current_user`` is None the real
    ``get_current_user`` runs — used by the no-auth case.
    """
    app = FastAPI()
    app.include_router(router)

    fake = _FakeSession()

    async def _override_db():
        yield fake

    app.dependency_overrides[get_db] = _override_db

    if current_user is not None:
        async def _override_current_user() -> CurrentUser:
            return current_user

        app.dependency_overrides[get_current_user] = _override_current_user

    return app, fake


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


# ── Router scaffold ──────────────────────────────────────


class TestRouterScaffold:
    """Module-level invariants the skeleton auto-mount depends on."""

    def test_router_is_apirouter(self):
        """``router`` must be a FastAPI ``APIRouter`` for auto-mount."""
        assert isinstance(router, APIRouter)

    def test_router_prefix_is_recipes(self):
        """Auto-mount adds /api/v1 — router declares only /recipes."""
        assert router.prefix == "/recipes"

    def test_delete_route_registered(self):
        """DELETE /recipes/{recipe_id} registered with status 204."""
        delete_routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/recipes/{recipe_id}"
            and "DELETE" in (getattr(r, "methods", set()) or set())
        ]
        assert delete_routes, "DELETE /recipes/{recipe_id} not registered"
        assert any(
            getattr(r, "status_code", None) == 204 for r in delete_routes
        ), "DELETE /recipes/{recipe_id} must declare status_code=204"

    def test_delete_route_uses_require_roles_dependency(self):
        """DELETE must depend on get_current_user via require_roles."""
        delete_routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/recipes/{recipe_id}"
            and "DELETE" in (getattr(r, "methods", set()) or set())
        ]
        assert delete_routes
        route = delete_routes[0]
        seen: list = []
        stack = [route.dependant]
        while stack:
            d = stack.pop()
            seen.append(d.call)
            stack.extend(d.dependencies)
        assert get_current_user in seen, (
            "DELETE /recipes/{recipe_id} must depend on get_current_user "
            "via require_roles"
        )


# ── Happy-path ───────────────────────────────────────────


class TestDeleteRecipeHappyPath:
    """Happy-path coverage of the delete_recipe capability."""

    async def test_happy_path_returns_204_with_empty_body(
        self, make_client, monkeypatch
    ):
        """Owner deletes → 204 with empty response body."""
        cu = _current_user()
        app, fake = _make_app(current_user=cu)

        captured: dict = {}

        def _fake_delete(session, *, recipe_id, owner_id):
            captured["recipe_id"] = recipe_id
            captured["owner_id"] = owner_id
            return True

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _fake_delete)

        client = await make_client(app)
        resp = await client.delete(f"/recipes/{RECIPE_UUID}")

        assert resp.status_code == 204, resp.text
        # 204 must have an empty body.
        assert resp.content == b""
        # Repository helper was called with the right scoping args.
        assert captured["recipe_id"] == RECIPE_UUID
        assert captured["owner_id"] == USER_A_UUID
        # AsyncSession.run_sync was indeed invoked (sync bridge used).
        assert len(fake.run_sync_calls) == 1

    async def test_admin_role_can_delete_their_own_recipe(
        self, make_client, monkeypatch
    ):
        """Admin role also accepted by ``require_roles(['user','admin'])``."""
        cu = _current_user(
            user_id=ADMIN_UUID, email=ADMIN_EMAIL, role="admin",
        )
        app, _ = _make_app(current_user=cu)

        captured: dict = {}

        def _fake_delete(session, *, recipe_id, owner_id):
            captured["owner_id"] = owner_id
            return True

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _fake_delete)

        client = await make_client(app)
        resp = await client.delete(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 204
        assert captured["owner_id"] == ADMIN_UUID

    async def test_handler_passes_jwt_owner_id_to_repo(
        self, make_client, monkeypatch
    ):
        """Handler contract: owner_id passed to repo == JWT.sub.

        owner_id MUST be derived from the authenticated identity, not
        from any client input. The path only carries recipe_id; this
        test locks the contract regardless.
        """
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        captured: dict = {}

        def _fake_delete(session, *, recipe_id, owner_id):
            captured["owner_id"] = owner_id
            captured["recipe_id"] = recipe_id
            return True

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _fake_delete)

        client = await make_client(app)
        resp = await client.delete(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 204
        assert captured["owner_id"] == USER_A_UUID
        assert captured["recipe_id"] == RECIPE_UUID


# ── 404 cases (absent / wrong owner — collapsed) ─────────


class TestDeleteRecipeNotFound:
    """404 cases: absent row OR wrong owner — both surface identically."""

    async def test_random_uuid_returns_404(self, make_client, monkeypatch):
        """Random recipe id with no matching row → 404 recipe_not_found."""
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        def _fake_delete(session, *, recipe_id, owner_id):
            return False

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _fake_delete)

        client = await make_client(app)
        resp = await client.delete(f"/recipes/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "recipe_not_found"}

    async def test_cross_user_returns_404_not_403(
        self, make_client, monkeypatch
    ):
        """User B deleting user A's recipe → 404 (NOT 403).

        Locks the existence-leak invariant: response body for a
        wrong-owner attempt must be identical to a true 404.
        """
        cu_b = _current_user(
            user_id=USER_B_UUID,
            email=USER_B_EMAIL,
            display_name=USER_B_DISPLAY_NAME,
        )
        app, _ = _make_app(current_user=cu_b)

        captured: dict = {}

        def _fake_delete(session, *, recipe_id, owner_id):
            # Repo DELETE WHERE id=:recipe_id AND owner_id=:owner_id
            # returns False (rowcount=0) when the row is owned by
            # someone else.
            captured["recipe_id"] = recipe_id
            captured["owner_id"] = owner_id
            return False

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _fake_delete)

        client = await make_client(app)
        # The recipe RECIPE_UUID belongs to user A (per test fixture);
        # user B is asking — the repo sees owner_id=USER_B_UUID and
        # returns False.
        resp = await client.delete(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 404
        # Body identical to absent-row case — no existence leak.
        assert resp.json() == {"detail": "recipe_not_found"}
        assert captured["owner_id"] == USER_B_UUID

    async def test_double_delete_second_returns_404(
        self, make_client, monkeypatch
    ):
        """Idempotency boundary: 1st DELETE → 204; 2nd → 404.

        After the first DELETE commits, the row is gone — the second
        DELETE matches zero rows and returns False, surfacing as 404.
        Clients should treat both 204 and 404 after a delete as 'gone'.
        """
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        # Simulate the row existing on the first call, then being gone
        # on the second.
        call_count = {"n": 0}

        def _fake_delete(session, *, recipe_id, owner_id):
            call_count["n"] += 1
            return call_count["n"] == 1

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _fake_delete)

        client = await make_client(app)
        first = await client.delete(f"/recipes/{RECIPE_UUID}")
        second = await client.delete(f"/recipes/{RECIPE_UUID}")

        assert first.status_code == 204
        assert second.status_code == 404
        assert second.json() == {"detail": "recipe_not_found"}
        assert call_count["n"] == 2


# ── Auth / role gating ───────────────────────────────────


class TestDeleteRecipeAuth:
    """401 / 403 cases from the require_roles dependency."""

    async def test_unauthenticated_returns_401(self, make_client):
        """No Authorization header → 401 from the real dependency."""
        app, _ = _make_app(current_user=None)
        client = await make_client(app)

        resp = await client.delete(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 401

    async def test_unauthenticated_does_not_call_repository(
        self, make_client, monkeypatch
    ):
        """A 401 short-circuit must never reach the DB layer."""
        called: list[str] = []

        def _boom_delete(session, *, recipe_id, owner_id):
            called.append("delete_recipe_for_owner")
            raise AssertionError(
                "delete_recipe_for_owner must not run on unauthenticated DELETE"
            )

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _boom_delete)

        app, _ = _make_app(current_user=None)
        client = await make_client(app)
        resp = await client.delete(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 401
        assert called == []

    async def test_super_admin_role_is_forbidden(
        self, make_client, monkeypatch
    ):
        """``super_admin`` is NOT in require_roles(['user','admin']) → 403."""
        cu = _current_user(role="super_admin")
        app, _ = _make_app(current_user=cu)

        called: list[str] = []

        def _boom_delete(session, *, recipe_id, owner_id):
            called.append("delete_recipe_for_owner")
            raise AssertionError("must not run for super_admin")

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _boom_delete)

        client = await make_client(app)
        resp = await client.delete(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 403
        assert called == []


# ── No mirror self-heal on DELETE ────────────────────────


class TestDeleteRecipeNoMirrorSelfHeal:
    """DELETE must NOT call ensure_local_user — FK enforced at INSERT only."""

    async def test_route_does_not_call_ensure_local_user(
        self, make_client, monkeypatch
    ):
        """DELETE must not invoke the mirror-upsert helper.

        The recipes-FK is enforced at INSERT time only; an existing
        recipe row implies owner_id already exists in users, and a
        non-existent row collapses to 404 either way. Patch
        ``recipes.ensure_local_user`` to explode; if the handler
        accidentally calls it the test fails loud.
        """
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        called: list[str] = []

        async def _boom(db, *, jwt_claims):
            called.append("ensure_local_user")
            raise AssertionError(
                "DELETE /recipes/{recipe_id} must not call ensure_local_user"
            )

        monkeypatch.setattr(recipes, "ensure_local_user", _boom)

        def _fake_delete(session, *, recipe_id, owner_id):
            return True

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _fake_delete)

        client = await make_client(app)
        resp = await client.delete(f"/recipes/{RECIPE_UUID}")
        assert resp.status_code == 204
        assert called == []


# ── Malformed path UUID ──────────────────────────────────


class TestDeleteRecipeMalformedUUID:
    """Non-UUID path segment → 422 (raw FastAPI); U7 collapses to 400."""

    async def test_malformed_uuid_rejected(self, make_client, monkeypatch):
        """A non-UUID recipe_id must be rejected before the repo runs."""
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        called: list[str] = []

        def _boom_delete(session, *, recipe_id, owner_id):
            called.append("delete_recipe_for_owner")
            raise AssertionError(
                "delete_recipe_for_owner must not run on malformed UUID"
            )

        monkeypatch.setattr(recipes, "delete_recipe_for_owner", _boom_delete)

        client = await make_client(app)
        resp = await client.delete("/recipes/not-a-uuid")
        # BE-006-U7 collapses 422 → 400. Accept either here.
        assert resp.status_code in (400, 422)
        assert called == []
