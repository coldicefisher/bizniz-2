"""Unit tests for the GET /api/v1/auth/me route (BE-010-U2).

Tests the 10 behaviors required by the BE-010 spec for ``/auth/me``:

1. ``test_get_me_happy_path`` — valid JWT + mirror row → 200 with the
   four UserOut fields.
2. ``test_get_me_role_from_jwt_not_db`` — DB row says ``role='admin'``,
   JWT says ``role='user'`` → response carries ``role='user'``. JWT is
   the authoritative source for authz; mirror's ``role`` column is a
   diagnostic snapshot only. Load-bearing sentinel matching BE-006-U7
   #24 and BE-008-U3 #21.
3. ``test_get_me_no_token_returns_401`` — no Authorization header → 401
   from the JWT dependency. Locks that the route IS protected; without
   this someone could remove ``Depends(get_current_user)`` and the
   regression would slip through.
4. ``test_get_me_db_operationalerror_returns_503`` — DB connection
   failure → 503 ``database_unavailable``.
5. ``test_get_me_db_generic_sqlerror_returns_503`` — any other
   SQLAlchemy error → 503 ``database_unavailable``.
6. ``test_get_me_user_row_missing_returns_500`` — race window where the
   middleware's auto-create succeeded but the row vanished →
   500 ``user_mirror_failed`` AND a structured ERROR log.
7. ``test_get_me_response_omits_sensitive_fields`` — response keys are
   EXACTLY ``{id, email, display_name, role}``. Uses ``==`` not subset,
   so if the User model ever grows a column that leaks into the
   response (``created_at``, ``password_hash``, etc.) this test fires.
8. ``test_get_me_response_display_name_can_be_null`` — null
   ``display_name`` round-trips as JSON null (not omitted, not empty
   string).
9. ``test_get_me_uses_db_display_name_not_jwt`` — JWT carries stale
   name claim, DB row has the updated name → response uses DB value.
   Per spec line 2: DB is authoritative for profile fields.
10. ``test_get_me_role_super_admin_preserved`` — full role precedence
    range works end-to-end.

Strategy: mount the ``/me`` router on a minimal FastAPI app and use
``app.dependency_overrides`` to swap ``get_current_user`` for a fixture
that returns the desired ``CurrentUser`` directly. This bypasses the
real JWT-validation pipeline (which is BE-006's responsibility and is
exercised by BE-006's own unit tests). Likewise ``get_db`` is
overridden with a sentinel session because the route never touches the
session itself — it just forwards to ``get_user_by_id``, which we
monkeypatch on the route module.
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

import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.api.routes import auth_me
from app.api.routes.auth_me import router
from app.core.auth import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.auth import ErrorResponse, UserOut


# ── Fixed test identity (the BE-010-U2 spec pins this) ───

FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
FIXED_EMAIL = "alice@example.com"
FIXED_DISPLAY_NAME = "Alice"


def _fake_user_row(
    user_id: uuid.UUID = FIXED_UUID,
    *,
    email: str = FIXED_EMAIL,
    display_name: str | None = FIXED_DISPLAY_NAME,
    role: str = "user",
) -> SimpleNamespace:
    """Build an object that quacks like a User ORM row.

    Only the four columns the route reads are populated, plus ``role``
    (deliberately distinct from the JWT role in some tests so we can
    assert the route picks the JWT role).
    """
    return SimpleNamespace(
        id=user_id,
        email=email,
        display_name=display_name,
        role=role,
    )


def _current_user(
    *,
    user_id: uuid.UUID = FIXED_UUID,
    email: str = FIXED_EMAIL,
    display_name: str | None = FIXED_DISPLAY_NAME,
    role: str = "user",
) -> CurrentUser:
    """Build a ``CurrentUser`` matching the spec fixture defaults."""
    return CurrentUser(
        id=user_id,
        email=email,
        display_name=display_name,
        role=role,
    )


def _make_app() -> FastAPI:
    """Mount the /me router on a bare FastAPI app for endpoint tests.

    We don't pull in ``app.main`` here because that triggers DB
    lifespan + auto-discovery of every other route, which would slow
    these unit tests and bind them to unrelated setup.
    """
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def app_factory():
    """Return a factory that yields a fresh app with overrides applied.

    Caller passes ``current_user=None`` to skip the get_current_user
    override (used by test_get_me_no_token_returns_401 to exercise the
    real auth dependency against a missing Authorization header).
    """

    apps_to_clear: list[FastAPI] = []

    def _factory(
        *,
        current_user: CurrentUser | None,
    ) -> FastAPI:
        app = _make_app()

        async def _override_db():
            # The handler never touches the session itself — it
            # forwards it to get_user_by_id, which the test
            # monkeypatches at the module level. A sentinel is enough
            # to prove "the session was threaded through". This MUST
            # be overridden even on the no-token path because FastAPI
            # resolves all dependencies of get_current_user before
            # the body runs, and the real get_db would try to connect
            # to Postgres.
            yield SimpleNamespace(__sentinel__="db_session")

        app.dependency_overrides[get_db] = _override_db
        if current_user is not None:

            async def _override_current_user() -> CurrentUser:
                return current_user

            app.dependency_overrides[get_current_user] = _override_current_user

        apps_to_clear.append(app)
        return app

    yield _factory

    for app in apps_to_clear:
        app.dependency_overrides.clear()


@pytest.fixture
async def make_client(app_factory):
    """Yield a factory that builds an ASGI AsyncClient for the given app."""
    clients: list[AsyncClient] = []

    async def _make(app: FastAPI) -> AsyncClient:
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        await client.__aenter__()
        clients.append(client)
        return client

    yield _make

    for client in clients:
        await client.__aexit__(None, None, None)


# ── Router scaffold & imports (regression guards) ────────


class TestRouterScaffold:
    """Module-level invariants — the SPA contract."""

    def test_router_is_apirouter(self):
        """``router`` must be a FastAPI ``APIRouter`` for auto-mount."""
        assert isinstance(router, APIRouter)

    def test_router_prefix_is_auth_only(self):
        """Auto-mount adds /api/v1 — router must declare only /auth."""
        assert router.prefix == "/auth"

    def test_router_tags_include_auth(self):
        """OpenAPI tags must include 'auth' for SPA codegen grouping."""
        assert router.tags == ["auth"]

    def test_me_route_registered_as_get(self):
        """GET /auth/me must be registered with status 200."""
        routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/auth/me"
        ]
        assert routes, "GET /auth/me not registered on the router"
        methods = set()
        for r in routes:
            methods |= set(getattr(r, "methods", set()) or set())
        assert "GET" in methods
        assert any(getattr(r, "status_code", None) == 200 for r in routes)

    def test_me_route_response_model_is_user_out(self):
        """The route's response_model must be ``UserOut``."""
        routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/auth/me"
        ]
        assert routes
        assert any(
            getattr(r, "response_model", None) is UserOut for r in routes
        )

    def test_me_route_documents_error_responses(self):
        """401/500/503 must be documented for OpenAPI codegen."""
        routes = [
            r for r in router.routes
            if getattr(r, "path", "") == "/auth/me"
        ]
        assert routes
        responses = {}
        for r in routes:
            responses.update(getattr(r, "responses", {}) or {})
        for code in (401, 500, 503):
            assert code in responses, f"missing {code} response doc"
            assert responses[code].get("model") is ErrorResponse


# ── The 10 BE-010-U2 spec cases ──────────────────────────


class TestGetMe:
    """The 10 spec-mandated test cases for GET /api/v1/auth/me."""

    async def test_get_me_happy_path(
        self, app_factory, make_client, monkeypatch
    ):
        """Case 1: valid JWT + mirror row → 200 with UserOut payload.

        Pre-insert a User row with id=current_user.id and matching
        fields; GET /auth/me; assert exact equality on the four
        UserOut keys.
        """
        cu = _current_user()
        row = _fake_user_row()

        async def _fake_get_user_by_id(db, user_id):
            assert user_id == FIXED_UUID
            return row

        monkeypatch.setattr(auth_me, "get_user_by_id", _fake_get_user_by_id)
        app = app_factory(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json() == {
            "id": str(FIXED_UUID),
            "email": FIXED_EMAIL,
            "display_name": FIXED_DISPLAY_NAME,
            "role": "user",
        }

    async def test_get_me_role_from_jwt_not_db(
        self, app_factory, make_client, monkeypatch
    ):
        """Case 2: role comes from the JWT, never from the DB row.

        Mirror is stale (says ``admin``); JWT says ``user``. Response
        MUST carry the JWT role. Load-bearing sentinel — three routes
        in a row assert this (BE-006-U7 #24, BE-008-U3 #21, here).
        Without it someone could regress to reading ``user_row.role``
        and silently break authz boundaries.
        """
        cu = _current_user(role="user")
        # Deliberately stale mirror: DB says admin, JWT says user.
        row = _fake_user_row(role="admin")

        async def _fake_get_user_by_id(db, user_id):
            return row

        monkeypatch.setattr(auth_me, "get_user_by_id", _fake_get_user_by_id)
        app = app_factory(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/auth/me")
        assert resp.status_code == 200
        # JWT wins, DB column ignored.
        assert resp.json()["role"] == "user"

    async def test_get_me_no_token_returns_401(
        self, app_factory, make_client
    ):
        """Case 3: no Authorization header → 401.

        Do NOT override get_current_user; call without a token; assert
        401. Locks that the route IS protected — without this test
        someone could accidentally remove ``Depends(get_current_user)``
        and authentication would silently disappear.
        """
        # current_user=None means we keep the real get_current_user.
        # The real dependency raises 401 unauthenticated when the
        # Authorization header is missing (_validate_token_shape).
        app = app_factory(current_user=None)
        client = await make_client(app)

        resp = await client.get("/auth/me")
        assert resp.status_code == 401

    async def test_get_me_db_operationalerror_returns_503(
        self, app_factory, make_client, monkeypatch
    ):
        """Case 4: ``OperationalError`` from get_user_by_id → 503.

        DB unreachable / connection refused surfaces as
        ``OperationalError``; the route must translate to 503 with the
        ``database_unavailable`` envelope so the SPA can render the
        right offline UX.
        """
        cu = _current_user()

        async def _fake_get_user_by_id(db, user_id):
            raise OperationalError(
                "SELECT 1", {}, Exception("connection refused")
            )

        monkeypatch.setattr(auth_me, "get_user_by_id", _fake_get_user_by_id)
        app = app_factory(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/auth/me")
        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "database_unavailable"}

    async def test_get_me_db_generic_sqlerror_returns_503(
        self, app_factory, make_client, monkeypatch
    ):
        """Case 5: any ``SQLAlchemyError`` → 503 database_unavailable.

        Catches the broad ``SQLAlchemyError`` hierarchy so transient DB
        issues (deadlocks, statement timeouts, etc.) don't bubble as
        500 — they're retryable from the SPA's perspective.
        """
        cu = _current_user()

        async def _fake_get_user_by_id(db, user_id):
            raise SQLAlchemyError("generic db blew up")

        monkeypatch.setattr(auth_me, "get_user_by_id", _fake_get_user_by_id)
        app = app_factory(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/auth/me")
        assert resp.status_code == 503
        assert resp.json()["detail"] == {"error": "database_unavailable"}

    async def test_get_me_user_row_missing_returns_500(
        self, app_factory, make_client, monkeypatch, caplog
    ):
        """Case 6: mirror row missing post-middleware → 500 + ERROR log.

        Narrow race: ``get_current_user`` claimed the row was created
        but our SELECT got nothing. Translate to 500
        ``user_mirror_failed`` AND emit
        ``user_mirror_missing_post_middleware`` at ERROR so an oncall
        engineer can find the race in logs.
        """
        cu = _current_user()

        async def _fake_get_user_by_id(db, user_id):
            return None  # the narrow race window the spec describes

        monkeypatch.setattr(auth_me, "get_user_by_id", _fake_get_user_by_id)
        app = app_factory(current_user=cu)
        client = await make_client(app)

        with caplog.at_level(logging.ERROR, logger=auth_me.logger.name):
            resp = await client.get("/auth/me")
        assert resp.status_code == 500
        assert resp.json()["detail"] == {"error": "user_mirror_failed"}

        # Structured log captured.
        records = [
            r for r in caplog.records
            if r.message == "user_mirror_missing_post_middleware"
        ]
        assert len(records) == 1, (
            "expected exactly one user_mirror_missing_post_middleware log, "
            f"got {[r.message for r in caplog.records]}"
        )
        assert getattr(records[0], "user_id") == str(FIXED_UUID)

    async def test_get_me_response_omits_sensitive_fields(
        self, app_factory, make_client, monkeypatch
    ):
        """Case 7: response keys are EXACTLY the four UserOut fields.

        Strict equality (``==``, not subset) so if the User model
        grows a new column (``display_name_normalized``,
        ``last_seen_at``, ``password_hash``, ``fa_registration``,
        ``role_db``, ``created_at``, ``updated_at`` …) and someone
        accidentally ``model_dump(user_row)``s it, this test fires.

        Also explicitly checks that none of the known-sensitive field
        names appear in the response — defense in depth, in case a
        future column is added with one of those exact names.
        """
        cu = _current_user()
        # Row carries extra attributes that MUST NOT leak.
        row = SimpleNamespace(
            id=FIXED_UUID,
            email=FIXED_EMAIL,
            display_name=FIXED_DISPLAY_NAME,
            role="user",
            password="should-not-leak",
            password_hash="$2b$12$should-not-leak",
            fa_registration={"applicationId": "leak"},
            jwt="ey.should.not.leak",
            token="should-not-leak",
            raw_claims={"sub": "leak"},
            role_db="admin",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )

        async def _fake_get_user_by_id(db, user_id):
            return row

        monkeypatch.setattr(auth_me, "get_user_by_id", _fake_get_user_by_id)
        app = app_factory(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/auth/me")
        assert resp.status_code == 200
        body = resp.json()

        # Strict equality on the key set — NO subset check.
        assert set(body.keys()) == {"id", "email", "display_name", "role"}

        # Explicit deny list for known-sensitive names.
        for forbidden in (
            "password",
            "password_hash",
            "fa_registration",
            "jwt",
            "token",
            "raw_claims",
            "role_db",
            "created_at",
            "updated_at",
        ):
            assert forbidden not in body, (
                f"sensitive field {forbidden!r} leaked into /me response"
            )

    async def test_get_me_response_display_name_can_be_null(
        self, app_factory, make_client, monkeypatch
    ):
        """Case 8: a null ``display_name`` round-trips as JSON null.

        Not omitted (Pydantic would otherwise drop None by default in
        some configs), not coerced to empty string. The SPA UI
        depends on the explicit null to render the "set a display
        name" prompt.
        """
        cu = _current_user(display_name=None)
        row = _fake_user_row(display_name=None)

        async def _fake_get_user_by_id(db, user_id):
            return row

        monkeypatch.setattr(auth_me, "get_user_by_id", _fake_get_user_by_id)
        app = app_factory(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        assert "display_name" in body, "display_name key was omitted"
        assert body["display_name"] is None

    async def test_get_me_uses_db_display_name_not_jwt(
        self, app_factory, make_client, monkeypatch
    ):
        """Case 9: DB is authoritative for profile fields.

        JWT carries a stale name claim (``Alice``) — the user updated
        their display_name to ``Alice (Updated)`` in the DB after the
        JWT was issued. Response must reflect the DB value, not the
        JWT value. Per spec line 2: "Read display_name from local
        users row (DB is authoritative for the mirrored profile
        fields)."
        """
        cu = _current_user(display_name="Alice")  # stale JWT name
        row = _fake_user_row(display_name="Alice (Updated)")  # fresh DB

        async def _fake_get_user_by_id(db, user_id):
            return row

        monkeypatch.setattr(auth_me, "get_user_by_id", _fake_get_user_by_id)
        app = app_factory(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Alice (Updated)"

    async def test_get_me_role_super_admin_preserved(
        self, app_factory, make_client, monkeypatch
    ):
        """Case 10: ``super_admin`` survives the full role precedence range.

        Cases 1 (user) and 2 (user) cover the low end; this case
        covers the top of ``_ROLE_PRECEDENCE`` so we know the entire
        range round-trips. The mirror's ``role`` column is left as
        ``user`` to also re-confirm the JWT-wins invariant at the
        top end.
        """
        cu = _current_user(role="super_admin")
        row = _fake_user_row(role="user")  # stale mirror — JWT still wins

        async def _fake_get_user_by_id(db, user_id):
            return row

        monkeypatch.setattr(auth_me, "get_user_by_id", _fake_get_user_by_id)
        app = app_factory(current_user=cu)
        client = await make_client(app)

        resp = await client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["role"] == "super_admin"
