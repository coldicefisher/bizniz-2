"""Unit tests for PUT /api/v1/recipes/{recipe_id} (BE-006-U5).

Covers the spec scenarios from the ``update_recipe`` capability:

1. ``happy_path`` — owner updates → 200 with the updated RecipeOut
   payload; updated_at advances past created_at.
2. ``cross_user_returns_404`` — user B tries to update user A's
   recipe → 404 (NOT 403); response body identical to the absent-row
   case (no existence leak).
3. ``not_found_404`` — random UUID → 404.
4. ``owner_id_immutable`` — body smuggling ``owner_id`` is rejected
   by ``RecipeCreate.extra='forbid'`` BEFORE reaching the handler;
   the handler's own contract (owner_id strictly from JWT.sub) is
   verified independently by observing what the helper receives.
5. ``unauthenticated_401`` — no Authorization header → 401.
6. ``ingredients_replaced_not_merged`` — full-replacement semantics
   (PUT, not PATCH); a smaller ingredients list overwrites the
   prior one entirely.
7. ``unicode_round_trip`` — emoji/CJK/RTL preserved byte-identical.
8. ``no_mirror_self_heal`` — PUT does NOT call ensure_local_user;
   FK is enforced at INSERT time only and the existing row already
   implies owner_id ∈ users.

Strategy mirrors ``test_recipes_post.py``: mount the ``/recipes``
router on a bare FastAPI app, override ``get_current_user`` /
``get_db`` via ``app.dependency_overrides``, and monkeypatch the
update helper so the handler can be exercised without a real
Postgres connection.

The PUT handler calls ``_update_recipe_for_owner`` (sync) through
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
from datetime import datetime, timedelta, timezone
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


def _valid_payload(**overrides) -> dict:
    """Return a baseline RecipeCreate body; ``overrides`` patch fields."""
    base = {
        "title": "Updated Recipe",
        "description": "Replacement description.",
        "ingredients": ["flour", "water", "salt"],
        "instructions": ["Mix.", "Bake."],
        "prep_time": 15,
        "cook_time": 25,
        "servings": 4,
    }
    base.update(overrides)
    return base


class _FakeSession:
    """AsyncSession stand-in that records calls and forwards run_sync.

    The PUT handler invokes ``await db.run_sync(lambda s: _update_recipe_for_owner(s, ...))``.
    We intercept the lambda by passing a dummy sync-session
    (``SimpleNamespace()``) and let the lambda run — but the inner
    helper is monkeypatched in each test, so the lambda never
    actually touches a real Session.
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


def _persisted_recipe(
    *,
    owner_id: uuid.UUID,
    payload: dict,
    recipe_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> SimpleNamespace:
    """Build a Recipe-shaped row matching the input payload + owner.

    SimpleNamespace mirrors the SQLAlchemy ORM attribute surface
    well enough for ``RecipeOut.model_validate(...)`` to project it
    via the ``from_attributes=True`` config on RecipeOut.
    """
    created = created_at or datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    updated = updated_at or (created + timedelta(seconds=30))
    return SimpleNamespace(
        id=recipe_id or RECIPE_UUID,
        owner_id=owner_id,
        title=payload["title"],
        description=payload["description"],
        ingredients=list(payload["ingredients"]),
        instructions=list(payload["instructions"]),
        prep_time=payload["prep_time"],
        cook_time=payload["cook_time"],
        servings=payload["servings"],
        created_at=created,
        updated_at=updated,
    )


# ── Router scaffold ──────────────────────────────────────


class TestRouterScaffold:
    """Module-level invariants the skeleton auto-mount depends on."""

    def test_router_is_apirouter(self):
        """``router`` must be a FastAPI ``APIRouter`` for auto-mount."""
        assert isinstance(router, APIRouter)

    def test_router_prefix_is_recipes(self):
        """Auto-mount adds /api/v1 — router declares only /recipes."""
        assert router.prefix == "/recipes"

    def test_put_route_registered(self):
        """PUT /recipes/{recipe_id} must be registered with status 200."""
        put_routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/recipes/{recipe_id}"
            and "PUT" in (getattr(r, "methods", set()) or set())
        ]
        assert put_routes, "PUT /recipes/{recipe_id} not registered"
        assert any(
            getattr(r, "status_code", None) == 200 for r in put_routes
        ), "PUT /recipes/{recipe_id} must declare status_code=200"

    def test_put_route_uses_require_roles_dependency(self):
        """PUT must depend on get_current_user via require_roles."""
        put_routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/recipes/{recipe_id}"
            and "PUT" in (getattr(r, "methods", set()) or set())
        ]
        assert put_routes
        route = put_routes[0]
        seen: list = []
        stack = [route.dependant]
        while stack:
            d = stack.pop()
            seen.append(d.call)
            stack.extend(d.dependencies)
        assert get_current_user in seen, (
            "PUT /recipes/{recipe_id} must depend on get_current_user "
            "via require_roles"
        )


# ── Happy-path ───────────────────────────────────────────


class TestUpdateRecipeHappyPath:
    """Happy-path coverage of the update_recipe capability."""

    async def test_happy_path_returns_200_with_updated_recipe(
        self, make_client, monkeypatch
    ):
        """Owner updates → 200 with the updated RecipeOut payload."""
        cu = _current_user()
        app, fake = _make_app(current_user=cu)

        body = _valid_payload(title="Renamed Title")
        created = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
        updated = created + timedelta(minutes=5)

        captured: dict = {}

        def _fake_update(session, *, recipe_id, owner_id, data):
            captured["recipe_id"] = recipe_id
            captured["owner_id"] = owner_id
            captured["data"] = data
            return _persisted_recipe(
                owner_id=owner_id,
                payload=data.model_dump(),
                recipe_id=recipe_id,
                created_at=created,
                updated_at=updated,
            )

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _fake_update)

        client = await make_client(app)
        resp = await client.put(f"/recipes/{RECIPE_UUID}", json=body)

        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["title"] == "Renamed Title"
        assert out["description"] == body["description"]
        assert out["ingredients"] == body["ingredients"]
        assert out["instructions"] == body["instructions"]
        assert out["prep_time"] == body["prep_time"]
        assert out["cook_time"] == body["cook_time"]
        assert out["servings"] == body["servings"]
        assert out["owner_id"] == str(USER_A_UUID)
        assert out["id"] == str(RECIPE_UUID)
        # updated_at must advance past created_at
        assert out["updated_at"] > out["created_at"]
        # Repository helper was called with the right scoping args.
        assert captured["recipe_id"] == RECIPE_UUID
        assert captured["owner_id"] == USER_A_UUID

    async def test_admin_role_can_update_their_own_recipe(
        self, make_client, monkeypatch
    ):
        """Admin role also accepted by ``require_roles(['user','admin'])``."""
        cu = _current_user(
            user_id=ADMIN_UUID, email=ADMIN_EMAIL, role="admin",
        )
        app, _ = _make_app(current_user=cu)

        def _fake_update(session, *, recipe_id, owner_id, data):
            return _persisted_recipe(
                owner_id=owner_id,
                payload=data.model_dump(),
                recipe_id=recipe_id,
            )

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _fake_update)

        client = await make_client(app)
        resp = await client.put(
            f"/recipes/{RECIPE_UUID}", json=_valid_payload()
        )
        assert resp.status_code == 200
        assert resp.json()["owner_id"] == str(ADMIN_UUID)

    async def test_unicode_round_trip(self, make_client, monkeypatch):
        """Unicode (emoji, accents, CJK) round-trips byte-identical."""
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        def _fake_update(session, *, recipe_id, owner_id, data):
            return _persisted_recipe(
                owner_id=owner_id,
                payload=data.model_dump(),
                recipe_id=recipe_id,
            )

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _fake_update)

        client = await make_client(app)
        body = _valid_payload(
            title="Soupe à l'oignon 🧅",
            description="美味しい — délicieux",
            ingredients=["🧅 onion", "fromage"],
            instructions=["みじん切り", "炒める"],
        )
        resp = await client.put(f"/recipes/{RECIPE_UUID}", json=body)
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["title"] == "Soupe à l'oignon 🧅"
        assert out["description"] == "美味しい — délicieux"
        assert out["ingredients"] == ["🧅 onion", "fromage"]
        assert out["instructions"] == ["みじん切り", "炒める"]

    async def test_ingredients_replaced_not_merged(
        self, make_client, monkeypatch
    ):
        """PUT semantics: a smaller list fully overwrites the prior one."""
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        def _fake_update(session, *, recipe_id, owner_id, data):
            return _persisted_recipe(
                owner_id=owner_id,
                payload=data.model_dump(),
                recipe_id=recipe_id,
            )

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _fake_update)

        client = await make_client(app)
        body = _valid_payload(ingredients=["only-one"])
        resp = await client.put(f"/recipes/{RECIPE_UUID}", json=body)
        assert resp.status_code == 200
        out = resp.json()
        assert out["ingredients"] == ["only-one"]
        assert len(out["ingredients"]) == 1


# ── Owner sourcing (immutable owner_id) ──────────────────


class TestUpdateRecipeOwnerImmutable:
    """owner_id is sourced strictly from JWT — body smuggling is rejected."""

    async def test_owner_id_in_body_rejected_as_extra(
        self, make_client, monkeypatch
    ):
        """RecipeCreate.extra='forbid' rejects owner_id in the body.

        BEFORE the handler runs. We patch the helper to assert it's
        never called on this path.
        """
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        called: list[str] = []

        def _boom(session, *, recipe_id, owner_id, data):
            called.append("update")
            raise AssertionError(
                "_update_recipe_for_owner must not run when body fails"
                " schema validation"
            )

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _boom)

        client = await make_client(app)
        body = _valid_payload()
        body["owner_id"] = str(uuid.uuid4())
        resp = await client.put(f"/recipes/{RECIPE_UUID}", json=body)
        assert resp.status_code in (400, 422)
        assert called == []

    async def test_helper_receives_jwt_owner_id_not_body(
        self, make_client, monkeypatch
    ):
        """Handler contract: owner_id passed to helper == JWT.sub."""
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        captured: dict = {}

        def _fake_update(session, *, recipe_id, owner_id, data):
            captured["owner_id"] = owner_id
            return _persisted_recipe(
                owner_id=owner_id,
                payload=data.model_dump(),
                recipe_id=recipe_id,
            )

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _fake_update)

        client = await make_client(app)
        resp = await client.put(
            f"/recipes/{RECIPE_UUID}", json=_valid_payload()
        )
        assert resp.status_code == 200
        assert captured["owner_id"] == USER_A_UUID


# ── 404 cases (absent / wrong owner — collapsed) ─────────


class TestUpdateRecipeNotFound:
    """404 cases: absent row OR wrong owner — both surface identically."""

    async def test_random_uuid_returns_404(self, make_client, monkeypatch):
        """Random recipe id with no matching row → 404 recipe_not_found."""
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        def _fake_update(session, *, recipe_id, owner_id, data):
            return None

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _fake_update)

        client = await make_client(app)
        resp = await client.put(
            f"/recipes/{uuid.uuid4()}", json=_valid_payload()
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "recipe_not_found"}

    async def test_cross_user_returns_404_not_403(
        self, make_client, monkeypatch
    ):
        """User B updating user A's recipe → 404 (NOT 403).

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

        def _fake_update(session, *, recipe_id, owner_id, data):
            # Helper SELECT WHERE id=:recipe_id AND owner_id=:owner_id
            # returns None when the row is owned by someone else.
            captured["recipe_id"] = recipe_id
            captured["owner_id"] = owner_id
            return None

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _fake_update)

        client = await make_client(app)
        # The recipe RECIPE_UUID belongs to user A (per test fixture);
        # user B is asking — the helper sees owner_id=USER_B_UUID and
        # returns None.
        resp = await client.put(
            f"/recipes/{RECIPE_UUID}", json=_valid_payload()
        )
        assert resp.status_code == 404
        # Body identical to absent-row case — no existence leak.
        assert resp.json() == {"detail": "recipe_not_found"}
        assert captured["owner_id"] == USER_B_UUID


# ── Auth / role gating ───────────────────────────────────


class TestUpdateRecipeAuth:
    """401 / 403 cases from the require_roles dependency."""

    async def test_unauthenticated_returns_401(self, make_client):
        """No Authorization header → 401 from the real dependency."""
        app, _ = _make_app(current_user=None)
        client = await make_client(app)

        resp = await client.put(
            f"/recipes/{RECIPE_UUID}", json=_valid_payload()
        )
        assert resp.status_code == 401

    async def test_unauthenticated_does_not_call_helper(
        self, make_client, monkeypatch
    ):
        """A 401 short-circuit must never reach the DB layer."""
        called: list[str] = []

        def _boom_update(session, *, recipe_id, owner_id, data):
            called.append("_update_recipe_for_owner")
            raise AssertionError(
                "_update_recipe_for_owner must not run on unauthenticated PUT"
            )

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _boom_update)

        app, _ = _make_app(current_user=None)
        client = await make_client(app)
        resp = await client.put(
            f"/recipes/{RECIPE_UUID}", json=_valid_payload()
        )
        assert resp.status_code == 401
        assert called == []

    async def test_super_admin_role_is_forbidden(self, make_client, monkeypatch):
        """``super_admin`` is NOT in require_roles(['user','admin']) → 403."""
        cu = _current_user(role="super_admin")
        app, _ = _make_app(current_user=cu)

        called: list[str] = []

        def _boom_update(session, *, recipe_id, owner_id, data):
            called.append("_update_recipe_for_owner")
            raise AssertionError("must not run for super_admin")

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _boom_update)

        client = await make_client(app)
        resp = await client.put(
            f"/recipes/{RECIPE_UUID}", json=_valid_payload()
        )
        assert resp.status_code == 403
        assert called == []


# ── No mirror self-heal on PUT ───────────────────────────


class TestUpdateRecipeNoMirrorSelfHeal:
    """PUT must NOT call ensure_local_user — FK enforced at INSERT only."""

    async def test_route_does_not_call_ensure_local_user(
        self, make_client, monkeypatch
    ):
        """Spec note: ensure_local_user not required here.

        The recipes-FK is enforced at INSERT time only; an existing
        recipe row implies owner_id already exists in users. Patch
        ``recipes.ensure_local_user`` to explode; if the handler
        accidentally calls it the test fails loud.
        """
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        called: list[str] = []

        async def _boom(db, *, jwt_claims):
            called.append("ensure_local_user")
            raise AssertionError(
                "PUT /recipes/{recipe_id} must not call ensure_local_user — "
                "FK is enforced at INSERT time only"
            )

        monkeypatch.setattr(recipes, "ensure_local_user", _boom)

        def _fake_update(session, *, recipe_id, owner_id, data):
            return _persisted_recipe(
                owner_id=owner_id,
                payload=data.model_dump(),
                recipe_id=recipe_id,
            )

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _fake_update)

        client = await make_client(app)
        resp = await client.put(
            f"/recipes/{RECIPE_UUID}", json=_valid_payload()
        )
        assert resp.status_code == 200
        assert called == []


# ── Body validation gating (RecipeCreate) ────────────────


class TestUpdateRecipeValidation:
    """Field-level validation rejections via RecipeCreate.

    These assert the *gate* (FastAPI/Pydantic rejects with 422) — the
    422→400 collapsing for the public capability lives in BE-006-U7
    and is out of scope for this unit. Accept either status here.
    """

    @pytest.fixture(autouse=True)
    def _block_helper_calls(self, monkeypatch):
        """Validation failures must never reach the helper."""

        def _boom_update(session, *, recipe_id, owner_id, data):
            raise AssertionError(
                "_update_recipe_for_owner called despite validation failure"
            )

        monkeypatch.setattr(recipes, "_update_recipe_for_owner", _boom_update)

    async def _put(self, make_client, body: dict, *, recipe_id=None):
        cu = _current_user()
        app, _ = _make_app(current_user=cu)
        client = await make_client(app)
        rid = recipe_id or RECIPE_UUID
        return await client.put(f"/recipes/{rid}", json=body)

    async def test_missing_description_rejected(self, make_client):
        body = _valid_payload()
        body.pop("description")
        resp = await self._put(make_client, body)
        assert resp.status_code in (400, 422)

    async def test_servings_zero_rejected(self, make_client):
        resp = await self._put(make_client, _valid_payload(servings=0))
        assert resp.status_code in (400, 422)

    async def test_empty_ingredients_rejected(self, make_client):
        resp = await self._put(make_client, _valid_payload(ingredients=[]))
        assert resp.status_code in (400, 422)

    async def test_whitespace_only_title_rejected(self, make_client):
        resp = await self._put(make_client, _valid_payload(title="   "))
        assert resp.status_code in (400, 422)

    async def test_extra_field_rejected(self, make_client):
        body = _valid_payload()
        body["tags"] = ["weeknight"]
        resp = await self._put(make_client, body)
        assert resp.status_code in (400, 422)
