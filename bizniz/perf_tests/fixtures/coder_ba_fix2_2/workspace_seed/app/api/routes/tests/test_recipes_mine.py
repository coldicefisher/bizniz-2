"""Unit tests for the GET /api/v1/recipes/mine route (BE-006-U3).

Covers the four spec scenarios from the ``list_my_recipes`` capability:

1. ``happy_path`` — authenticated user → 200 with body == [].
2. ``admin_same_response`` — authenticated admin → 200 with the same
   body == []. Admin moderation view ships later; in M2 admin sees the
   same empty list a regular user sees.
3. ``no_auth_401`` — no Authorization header → 401 from the real
   ``get_current_user`` dependency (we do NOT override it on this
   case, so the real JWT-validation pipeline runs and rejects the
   missing header).
4. ``two_users_isolated`` — user A and user B both call the route and
   both receive their own list; the repository receives DISTINCT
   ``owner_id`` arguments — proving the session identity actually
   changes between distinct JWTs. Without this a regression that
   wired the route to a process-global "current user" would still
   pass cases 1-3.

Strategy mirrors the ``test_recipes_post.py`` test pattern: mount
the ``/recipes`` router on a bare FastAPI app, override
``get_current_user`` via ``app.dependency_overrides`` to inject the
desired CurrentUser, override ``get_db`` with a sentinel
``SimpleNamespace`` (the route never inspects it because the
repository function is monkeypatched), monkeypatch
``recipes.list_recipes_for_owner`` to record the ``owner_id`` it
was called with and return a deterministic result. This keeps the
unit test hermetic — no real Postgres connection, no JWT issuance.

M2 behavior contract:
* The handler DOES call the DB (via ``list_recipes_for_owner``) —
  this is a change from the M1 stub that always returned [].
* The handler does NOT perform a mirror self-heal upsert
  (``ensure_local_user``) — the list endpoint is read-only and has
  no FK dependency on users. Verified by
  ``test_route_does_not_call_ensure_local_user``.
"""
# Settings() requires FUSIONAUTH_TENANT_ID / _APPLICATION_ID / _API_KEY
# at import time. Fill in safe defaults before app.core.config is
# imported transitively by anything below. setdefault preserves a real
# env var if one is present.
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


# ── Fixed test identities (two distinct users for isolation) ─

USER_A_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_A_EMAIL = "userA@example.com"
USER_A_DISPLAY_NAME = "Alice"

USER_B_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_B_EMAIL = "userB@example.com"
USER_B_DISPLAY_NAME = "Bob"

ADMIN_UUID = uuid.UUID("33333333-3333-3333-3333-333333333333")
ADMIN_EMAIL = "admin@example.com"
ADMIN_DISPLAY_NAME = "Admin"


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


def _make_app(*, current_user: CurrentUser | None) -> FastAPI:
    """Mount the /recipes router on a bare FastAPI app.

    When ``current_user`` is provided, ``get_current_user`` is swapped
    via ``dependency_overrides`` to bypass the real JWT pipeline and
    inject the test identity directly. When ``current_user`` is None
    the real dependency runs — used by the no_auth_401 case to assert
    that missing headers truly produce 401.

    ``get_db`` is overridden with a sentinel SimpleNamespace; the
    M2 handler forwards the session to ``list_recipes_for_owner``
    which the scenario tests monkeypatch — the sentinel is never
    introspected.
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
    """Factory yielding ASGI clients bound to the test apps."""
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
        """Auto-mount adds /api/v1 — router must declare only /recipes."""
        assert router.prefix == "/recipes"

    def test_router_tags_include_recipes(self):
        """OpenAPI tags must include 'recipes' for SPA codegen grouping."""
        assert router.tags == ["recipes"]

    def test_mine_route_registered_as_get(self):
        """GET /recipes/mine must be registered with status 200."""
        routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/recipes/mine"
        ]
        assert routes, "GET /recipes/mine not registered on the router"
        methods: set[str] = set()
        for r in routes:
            methods |= set(getattr(r, "methods", set()) or set())
        assert "GET" in methods
        assert any(getattr(r, "status_code", None) == 200 for r in routes)

    def test_route_uses_get_current_user_dependency(self):
        """Auth gate: the route must depend on ``get_current_user``.

        Without this guard a future refactor could accidentally drop
        ``Depends(get_current_user)`` and the no_auth_401 case below
        would still pass against a stubbed app — this scaffold-level
        assertion catches the regression at import time.
        """
        routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/recipes/mine"
        ]
        assert routes
        route = routes[0]
        # Walk the dependant tree for get_current_user
        dep = route.dependant
        seen: list = []
        stack = [dep]
        while stack:
            d = stack.pop()
            seen.append(d.call)
            stack.extend(d.dependencies)
        assert get_current_user in seen, (
            "GET /recipes/mine must depend on get_current_user "
            "(directly or transitively)"
        )


# ── The four BA-fix1-2 spec scenarios ────────────────────


def _patch_list_repo(
    monkeypatch,
    *,
    result: list | None = None,
    capture: list | None = None,
):
    """Monkeypatch ``recipes.list_recipes_for_owner`` to a recording stub.

    Returns an empty list by default; pass ``result`` to substitute a
    specific payload. When ``capture`` is provided, every call appends
    the ``owner_id`` argument it received — used by the isolation test
    to assert each request was scoped to its own caller.
    """
    payload = [] if result is None else result

    async def _fake_list(session, *, owner_id):
        if capture is not None:
            capture.append(owner_id)
        return payload

    monkeypatch.setattr(recipes, "list_recipes_for_owner", _fake_list)
    return _fake_list


class TestListMyRecipesScenarios:
    """The four spec scenarios from ``list_my_recipes``."""

    async def test_happy_path_user_returns_200_empty_list(
        self, make_client, monkeypatch
    ):
        """Scenario 1: authenticated user → 200, body == [].

        Repository returns []; handler projects it through
        ``response_model=list[RecipeOut]`` which serialises to an
        empty JSON array.
        """
        _patch_list_repo(monkeypatch)
        cu = _current_user(role="user")
        app = _make_app(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/recipes/mine")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_admin_same_response(self, make_client, monkeypatch):
        """Scenario 2: admin token → 200, body == [] (same as user).

        The admin moderation view ships later; in M2 the admin role
        gets the exact same payload a regular user gets — owner-scoped
        to the admin's own JWT.sub. Asserting on ``== []`` (not just
        status 200) locks the shape so a future admin branch can't
        silently change the response for non-admin callers.
        """
        _patch_list_repo(monkeypatch)
        admin = _current_user(
            user_id=ADMIN_UUID,
            email=ADMIN_EMAIL,
            display_name=ADMIN_DISPLAY_NAME,
            role="admin",
        )
        app = _make_app(current_user=admin)
        client = await make_client(app)

        resp = await client.get("/recipes/mine")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_no_auth_returns_401(self, make_client):
        """Scenario 3: no Authorization header → 401.

        Do NOT override get_current_user; call without a token; assert
        401. Locks that the route IS protected — without this test
        someone could accidentally remove ``Depends(get_current_user)``
        and authentication would silently disappear.
        """
        # current_user=None means we keep the real get_current_user;
        # the real dependency raises 401 on missing Authorization.
        app = _make_app(current_user=None)
        client = await make_client(app)

        resp = await client.get("/recipes/mine")
        assert resp.status_code == 401

    async def test_two_users_isolated(self, make_client, monkeypatch):
        """Scenario 4: A and B each scoped to their own owner_id.

        Two distinct CurrentUser fixtures, two separate test apps
        (each with its own dependency override). The repository stub
        records the ``owner_id`` it was called with on each request,
        proving the session identity actually changes between
        requests — without this a regression that hard-coded a
        process-global "current user" could still pass scenarios 1-3
        (both would see the same global). The captured ``owner_id``
        list is the load-bearing sentinel.
        """
        captured: list = []
        _patch_list_repo(monkeypatch, capture=captured)

        user_a = _current_user(
            user_id=USER_A_UUID,
            email=USER_A_EMAIL,
            display_name=USER_A_DISPLAY_NAME,
        )
        user_b = _current_user(
            user_id=USER_B_UUID,
            email=USER_B_EMAIL,
            display_name=USER_B_DISPLAY_NAME,
        )

        # ── User A ──
        app_a = _make_app(current_user=user_a)
        client_a = await make_client(app_a)
        recipes_a = await client_a.get("/recipes/mine")
        assert recipes_a.status_code == 200
        assert recipes_a.json() == []

        # ── User B ──
        app_b = _make_app(current_user=user_b)
        client_b = await make_client(app_b)
        recipes_b = await client_b.get("/recipes/mine")
        assert recipes_b.status_code == 200
        assert recipes_b.json() == []

        # Both /recipes/mine responses are the empty list.
        assert recipes_a.json() == recipes_b.json() == []

        # But the repository was invoked with DISTINCT owner_ids —
        # the load-bearing isolation assertion. Without this, a
        # regression that hard-coded a process-global "current user"
        # would still see both responses as [] and pass scenarios 1-3.
        assert captured == [USER_A_UUID, USER_B_UUID]


# ── Edge cases the capability spec calls out ─────────────


class TestListMyRecipesEdgeCases:
    """Edge cases from the capability spec."""

    async def test_query_params_are_ignored(self, make_client, monkeypatch):
        """Spec edge case: query params present (?limit=10) → ignored.

        The M2 endpoint accepts no declared query inputs; arbitrary
        query strings must NOT cause 4xx and must NOT change the
        response shape (pagination ships later — the spec is
        explicit: "do not reject, just ignore").
        """
        _patch_list_repo(monkeypatch)
        cu = _current_user()
        app = _make_app(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/recipes/mine?limit=10&offset=5&owner=bob")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_super_admin_role_is_forbidden(self, make_client, monkeypatch):
        """Spec auth: only ``user`` and ``admin`` reach this handler.

        ``super_admin`` is not in the ``require_roles(['user',
        'admin'])`` allowed set, so the role-gate dependency raises
        403 before the handler body runs. Locks the role list:
        widening it to include ``super_admin`` would be a behavior
        change that this test forces to be a conscious update.
        """
        _patch_list_repo(monkeypatch)
        cu = _current_user(role="super_admin")
        app = _make_app(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/recipes/mine")
        assert resp.status_code == 403

    async def test_route_does_not_call_ensure_local_user(
        self, make_client, monkeypatch
    ):
        """M2 invariant: the list handler MUST NOT self-heal the owner mirror.

        The spec says: "Do NOT call ensure_local_user here — list is
        read-only, no FK dependency." Patch
        ``recipes.ensure_local_user`` to explode; if the handler
        accidentally calls it the test fails loud. The POST handler
        (BE-006-U2) is where the self-heal lives — keeping it off
        the read path avoids needless write traffic and the
        "duplicate_email_in_mirror" 500 surface area on a GET.
        """
        _patch_list_repo(monkeypatch)
        cu = _current_user()
        called: list[str] = []

        async def _boom(db, *, jwt_claims):
            called.append("ensure_local_user")
            raise AssertionError(
                "GET /recipes/mine must not call ensure_local_user — "
                "the list endpoint is read-only with no FK dependency"
            )

        monkeypatch.setattr(recipes, "ensure_local_user", _boom)

        app = _make_app(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/recipes/mine")
        assert resp.status_code == 200
        assert resp.json() == []
        assert called == [], (
            "/recipes/mine called ensure_local_user — list endpoint "
            "must not perform mirror self-heal"
        )

    async def test_returns_repository_results_serialised_as_recipe_out(
        self, make_client, monkeypatch
    ):
        """End-to-end shape: repository rows project through RecipeOut.

        Stub ``list_recipes_for_owner`` to return two
        ``SimpleNamespace`` rows with the full Recipe-shaped attribute
        surface; assert that the JSON body carries the same fields,
        in the same DESC order, with the same values. Locks the
        ``response_model=list[RecipeOut]`` projection so a future
        rename of a Recipe field would break this test before it
        breaks the SPA codegen.
        """
        import uuid as _uuid
        from datetime import datetime, timezone

        cu = _current_user()
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
        earlier = datetime(2026, 5, 17, 11, 0, 0, tzinfo=timezone.utc)

        rows = [
            SimpleNamespace(
                id=_uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                owner_id=USER_A_UUID,
                title="Newest",
                description="latest",
                ingredients=["a", "b"],
                instructions=["step1"],
                prep_time=10,
                cook_time=20,
                servings=2,
                created_at=now,
                updated_at=now,
            ),
            SimpleNamespace(
                id=_uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                owner_id=USER_A_UUID,
                title="Older",
                description="earlier one",
                ingredients=["c"],
                instructions=["step1", "step2"],
                prep_time=5,
                cook_time=0,
                servings=1,
                created_at=earlier,
                updated_at=earlier,
            ),
        ]

        _patch_list_repo(monkeypatch, result=rows)

        app = _make_app(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/recipes/mine")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 2
        # Order preserved as returned by repository (newest-first).
        assert body[0]["title"] == "Newest"
        assert body[1]["title"] == "Older"
        # Full RecipeOut projection — every field present.
        for item in body:
            assert {
                "id", "owner_id", "title", "description", "ingredients",
                "instructions", "prep_time", "cook_time", "servings",
                "created_at", "updated_at",
            }.issubset(item.keys())
        assert body[0]["owner_id"] == str(USER_A_UUID)

    async def test_repository_called_with_authenticated_user_id(
        self, make_client, monkeypatch
    ):
        """Owner-scoping: ``list_recipes_for_owner(owner_id=user.id)``.

        Captures the ``owner_id`` keyword argument the route passes
        to the repository and asserts it equals the
        ``get_current_user`` override's ``id`` — proving owner is
        sourced strictly from the JWT identity, not from any
        client-supplied query or header.
        """
        captured: list = []
        _patch_list_repo(monkeypatch, capture=captured)

        cu = _current_user()
        app = _make_app(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/recipes/mine")
        assert resp.status_code == 200
        assert captured == [USER_A_UUID]
