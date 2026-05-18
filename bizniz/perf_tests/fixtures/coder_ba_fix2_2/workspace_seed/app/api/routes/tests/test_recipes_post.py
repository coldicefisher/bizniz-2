"""Unit tests for POST /api/v1/recipes (BE-006-U2).

Strategy mirrors ``test_recipes_mine.py``: mount the ``/recipes``
router on a bare FastAPI app, override ``get_current_user`` /
``get_db`` via ``app.dependency_overrides``, and monkeypatch the
repository / self-heal helpers so the handler can be exercised
without a real Postgres connection.

The repository's ``create_recipe`` is a SYNC function the handler
calls through ``db.run_sync``. The test session is a
``SimpleNamespace`` that intercepts ``run_sync`` and invokes the
lambda with a stub sync-session — that way we observe exactly what
the route does instead of poking ORM internals.
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


USER_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_EMAIL = "user@example.com"
USER_DISPLAY_NAME = "Alice"

ADMIN_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ADMIN_EMAIL = "admin@example.com"


def _current_user(
    *,
    user_id: uuid.UUID = USER_UUID,
    email: str = USER_EMAIL,
    display_name: str | None = USER_DISPLAY_NAME,
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
        "title": "Test Recipe",
        "description": "A delicious test recipe.",
        "ingredients": ["flour", "water", "salt"],
        "instructions": ["Mix.", "Bake."],
        "prep_time": 10,
        "cook_time": 20,
        "servings": 4,
    }
    base.update(overrides)
    return base


class _FakeSession:
    """AsyncSession stand-in that records calls and forwards run_sync.

    ``ensure_local_user`` calls ``get_user_by_id`` and (optionally)
    ``session.run_sync`` to upsert the mirror; the repository's
    ``create_recipe`` is invoked via ``await db.run_sync(lambda s:
    create_recipe(s, ...))``. We intercept the lambda by passing a
    dummy sync-session (``SimpleNamespace()``) and let the lambda run
    — but the inner repository function is monkeypatched in each
    test, so the lambda never actually touches a real Session.
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
    """Mount the /recipes router on a bare app with auth/db overrides.

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
) -> SimpleNamespace:
    """Build a Recipe-shaped row matching the input payload + owner.

    SimpleNamespace mirrors the SQLAlchemy ORM attribute surface
    well enough for ``RecipeOut.model_validate(...)`` to project it
    via the ``from_attributes=True`` config on RecipeOut.
    """
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=recipe_id or uuid.uuid4(),
        owner_id=owner_id,
        title=payload["title"],
        description=payload["description"],
        ingredients=list(payload["ingredients"]),
        instructions=list(payload["instructions"]),
        prep_time=payload["prep_time"],
        cook_time=payload["cook_time"],
        servings=payload["servings"],
        created_at=now,
        updated_at=now,
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

    def test_post_route_registered(self):
        """POST /recipes must be registered with status 201."""
        post_routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/recipes"
            and "POST" in (getattr(r, "methods", set()) or set())
        ]
        assert post_routes, "POST /recipes not registered"
        assert any(
            getattr(r, "status_code", None) == 201 for r in post_routes
        ), "POST /recipes must declare status_code=201"

    def test_post_route_uses_require_roles_dependency(self):
        """POST /recipes must depend on require_roles (auth + role gate)."""
        post_routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/recipes"
            and "POST" in (getattr(r, "methods", set()) or set())
        ]
        assert post_routes
        route = post_routes[0]
        # Walk the dependant tree for get_current_user (require_roles
        # composes it).
        seen: list = []
        stack = [route.dependant]
        while stack:
            d = stack.pop()
            seen.append(d.call)
            stack.extend(d.dependencies)
        assert get_current_user in seen, (
            "POST /recipes must depend on get_current_user via "
            "require_roles"
        )


# ── Happy-path + isolation ───────────────────────────────


class TestCreateRecipeHappyPath:
    """Happy-path coverage of the create_recipe capability."""

    async def test_happy_path_returns_201_with_full_recipe(
        self, make_client, monkeypatch
    ):
        """Authenticated user → 201 with full RecipeOut payload."""
        cu = _current_user()
        app, fake = _make_app(current_user=cu)

        async def _fake_ensure(db, *, jwt_claims):
            assert db is fake
            assert jwt_claims["sub"] == str(USER_UUID)
            assert jwt_claims["email"] == USER_EMAIL
            return USER_UUID

        recorded: dict = {}

        def _fake_create(session, *, owner_id, data):
            recorded["owner_id"] = owner_id
            recorded["payload"] = data
            return _persisted_recipe(owner_id=owner_id, payload=data.model_dump())

        monkeypatch.setattr(recipes, "ensure_local_user", _fake_ensure)
        monkeypatch.setattr(recipes, "create_recipe", _fake_create)

        client = await make_client(app)
        body = _valid_payload()
        resp = await client.post("/recipes", json=body)

        assert resp.status_code == 201, resp.text
        out = resp.json()
        assert out["title"] == body["title"]
        assert out["description"] == body["description"]
        assert out["ingredients"] == body["ingredients"]
        assert out["instructions"] == body["instructions"]
        assert out["prep_time"] == body["prep_time"]
        assert out["cook_time"] == body["cook_time"]
        assert out["servings"] == body["servings"]
        assert out["owner_id"] == str(USER_UUID)
        assert uuid.UUID(out["id"])  # parseable
        assert "created_at" in out
        assert "updated_at" in out
        assert recorded["owner_id"] == USER_UUID

    async def test_admin_role_can_create(self, make_client, monkeypatch):
        """Admin role also accepted by ``require_roles(['user','admin'])``."""
        cu = _current_user(
            user_id=ADMIN_UUID, email=ADMIN_EMAIL, role="admin",
        )
        app, _ = _make_app(current_user=cu)

        async def _fake_ensure(db, *, jwt_claims):
            return ADMIN_UUID

        def _fake_create(session, *, owner_id, data):
            return _persisted_recipe(owner_id=owner_id, payload=data.model_dump())

        monkeypatch.setattr(recipes, "ensure_local_user", _fake_ensure)
        monkeypatch.setattr(recipes, "create_recipe", _fake_create)

        client = await make_client(app)
        resp = await client.post("/recipes", json=_valid_payload())
        assert resp.status_code == 201
        assert resp.json()["owner_id"] == str(ADMIN_UUID)

    async def test_unicode_preserved(self, make_client, monkeypatch):
        """Unicode (emoji, accents, CJK) round-trips byte-identical."""
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        async def _fake_ensure(db, *, jwt_claims):
            return USER_UUID

        def _fake_create(session, *, owner_id, data):
            return _persisted_recipe(owner_id=owner_id, payload=data.model_dump())

        monkeypatch.setattr(recipes, "ensure_local_user", _fake_ensure)
        monkeypatch.setattr(recipes, "create_recipe", _fake_create)

        client = await make_client(app)
        body = _valid_payload(
            title="Soupe à l'oignon 🧅",
            description="美味しい — délicieux",
            ingredients=["🧅 onion", "fromage"],
            instructions=["みじん切り", "炒める"],
        )
        resp = await client.post("/recipes", json=body)
        assert resp.status_code == 201, resp.text
        out = resp.json()
        assert out["title"] == "Soupe à l'oignon 🧅"
        assert out["description"] == "美味しい — délicieux"
        assert out["ingredients"] == ["🧅 onion", "fromage"]
        assert out["instructions"] == ["みじん切り", "炒める"]

    async def test_owner_id_from_jwt_not_from_payload(
        self, make_client, monkeypatch
    ):
        """Even if client smuggles owner_id, JWT identity wins.

        ``RecipeCreate``'s ``extra='forbid'`` blocks an owner_id in
        the body BEFORE the handler sees it (422). The handler's
        own contract — owner_id sourced strictly from
        ``ensure_local_user`` — is verified independently by
        observing what is actually passed to the repository.
        """
        cu = _current_user()
        app, _ = _make_app(current_user=cu)

        captured: dict = {}

        async def _fake_ensure(db, *, jwt_claims):
            captured["sub"] = jwt_claims["sub"]
            return USER_UUID

        def _fake_create(session, *, owner_id, data):
            captured["owner_id_arg"] = owner_id
            return _persisted_recipe(owner_id=owner_id, payload=data.model_dump())

        monkeypatch.setattr(recipes, "ensure_local_user", _fake_ensure)
        monkeypatch.setattr(recipes, "create_recipe", _fake_create)

        client = await make_client(app)
        resp = await client.post("/recipes", json=_valid_payload())
        assert resp.status_code == 201
        assert captured["sub"] == str(USER_UUID)
        assert captured["owner_id_arg"] == USER_UUID


# ── Auth / role gating ───────────────────────────────────


class TestCreateRecipeAuth:
    """401 / 403 cases from the require_roles dependency."""

    async def test_unauthenticated_returns_401(self, make_client):
        """No Authorization header → 401 from the real dependency."""
        app, _ = _make_app(current_user=None)
        client = await make_client(app)

        resp = await client.post("/recipes", json=_valid_payload())
        assert resp.status_code == 401

    async def test_unauthenticated_does_not_call_repository(
        self, make_client, monkeypatch
    ):
        """A 401 short-circuit must never reach the DB layer."""
        called: list[str] = []

        async def _boom_ensure(db, *, jwt_claims):
            called.append("ensure_local_user")
            raise AssertionError(
                "ensure_local_user must not run on unauthenticated POST"
            )

        def _boom_create(session, *, owner_id, data):
            called.append("create_recipe")
            raise AssertionError(
                "create_recipe must not run on unauthenticated POST"
            )

        monkeypatch.setattr(recipes, "ensure_local_user", _boom_ensure)
        monkeypatch.setattr(recipes, "create_recipe", _boom_create)

        app, _ = _make_app(current_user=None)
        client = await make_client(app)
        resp = await client.post("/recipes", json=_valid_payload())
        assert resp.status_code == 401
        assert called == []


# ── Body validation (RecipeCreate) ───────────────────────


class TestCreateRecipeValidation:
    """Field-level validation rejections via RecipeCreate.

    These assert the *gate* (FastAPI/Pydantic rejects with 422) — the
    422→400 collapsing for the public capability lives in BE-006-U7
    and is out of scope for this unit. Accept either status here.
    """

    @pytest.fixture(autouse=True)
    def _block_db_calls(self, monkeypatch):
        """Validation failures must never reach the repository."""

        async def _boom_ensure(db, *, jwt_claims):
            raise AssertionError(
                "ensure_local_user called despite body validation failure"
            )

        def _boom_create(session, *, owner_id, data):
            raise AssertionError(
                "create_recipe called despite body validation failure"
            )

        monkeypatch.setattr(recipes, "ensure_local_user", _boom_ensure)
        monkeypatch.setattr(recipes, "create_recipe", _boom_create)

    async def _post(self, make_client, body: dict):
        cu = _current_user()
        app, _ = _make_app(current_user=cu)
        client = await make_client(app)
        return await client.post("/recipes", json=body)

    async def test_missing_title_rejected(self, make_client):
        body = _valid_payload()
        body.pop("title")
        resp = await self._post(make_client, body)
        assert resp.status_code in (400, 422)

    async def test_missing_description_rejected(self, make_client):
        body = _valid_payload()
        body.pop("description")
        resp = await self._post(make_client, body)
        assert resp.status_code in (400, 422)

    async def test_empty_ingredients_rejected(self, make_client):
        resp = await self._post(make_client, _valid_payload(ingredients=[]))
        assert resp.status_code in (400, 422)

    async def test_whitespace_only_ingredient_rejected(self, make_client):
        resp = await self._post(
            make_client, _valid_payload(ingredients=["  "])
        )
        assert resp.status_code in (400, 422)

    async def test_empty_instructions_rejected(self, make_client):
        resp = await self._post(make_client, _valid_payload(instructions=[]))
        assert resp.status_code in (400, 422)

    async def test_whitespace_only_title_rejected(self, make_client):
        resp = await self._post(make_client, _valid_payload(title="   "))
        assert resp.status_code in (400, 422)

    async def test_oversize_title_rejected(self, make_client):
        resp = await self._post(make_client, _valid_payload(title="x" * 201))
        assert resp.status_code in (400, 422)

    async def test_negative_prep_time_rejected(self, make_client):
        resp = await self._post(make_client, _valid_payload(prep_time=-1))
        assert resp.status_code in (400, 422)

    async def test_too_large_prep_time_rejected(self, make_client):
        resp = await self._post(make_client, _valid_payload(prep_time=1441))
        assert resp.status_code in (400, 422)

    async def test_servings_zero_rejected(self, make_client):
        resp = await self._post(make_client, _valid_payload(servings=0))
        assert resp.status_code in (400, 422)

    async def test_servings_too_large_rejected(self, make_client):
        resp = await self._post(make_client, _valid_payload(servings=1001))
        assert resp.status_code in (400, 422)

    async def test_integer_strict_rejects_string_prep_time(self, make_client):
        """strict=True on RecipeCreate forbids string→int coercion."""
        resp = await self._post(make_client, _valid_payload(prep_time="5"))
        assert resp.status_code in (400, 422)

    async def test_extra_field_rejected(self, make_client):
        """extra='forbid' rejects unknown fields (incl. client owner_id)."""
        body = _valid_payload()
        body["owner_id"] = str(uuid.uuid4())
        resp = await self._post(make_client, body)
        assert resp.status_code in (400, 422)

    async def test_extra_arbitrary_field_rejected(self, make_client):
        body = _valid_payload()
        body["tags"] = ["weeknight"]
        resp = await self._post(make_client, body)
        assert resp.status_code in (400, 422)
