"""Integration tests for the BE-006-fix1 error envelope + audit logging.

Verifies the cross-cutting contract that every non-2xx response from
the recipes router travels in the standard envelope shape::

    {
      "error": "<machine_code>",
      "message": "<human readable>",
      "field_errors": {"<field>": "<msg>", ...}   # validation 400s only
    }

and that successful create/update/delete handlers emit a structured
audit log line with ``event``, ``user_id``, ``recipe_id``,
``request_id`` and ``ts`` fields.

These tests mount the ACTUAL ``app.main.app`` (not a bare FastAPI
instance like the unit tests in ``app/api/routes/tests/``) so the
three globally-registered exception handlers
(``RequestValidationError`` → 400, ``HTTPException`` → reshape,
``OperationalError`` → 503) run end-to-end. The router-level unit
tests use bare apps and therefore continue to assert the raw
``{"detail": ...}`` shape — that's correct: those tests target the
router contract; this file targets the app-level envelope contract.

Strategy
--------
* Use ``app.dependency_overrides`` to bypass auth (``get_current_user``)
  and database I/O (``get_db``).
* Monkeypatch the repository imports on
  ``app.api.routes.recipes`` so the route's call sites resolve to
  test doubles — the same hooking pattern the unit tests use.
* For the 503 path, the repository double raises
  ``sqlalchemy.exc.OperationalError``; the app-level handler
  intercepts and remaps.
* For the audit log assertions, ``caplog`` captures records emitted
  by the ``app.api.routes.recipes`` logger. The audit payload is a
  JSON document on the log record's message, so each assertion
  decodes it and inspects the fields.

Cleanup
-------
``app.dependency_overrides`` is process-global; the ``app_with_user``
fixture clears all overrides after every test so subsequent tests
(in this or any other file) start from a clean slate.
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

import json
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from app.api.routes import recipes as recipes_module
from app.core.auth import CurrentUser, get_current_user
from app.db.session import get_db
from app.main import app


# ── Fixed test identity ──────────────────────────────────

USER_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_EMAIL = "user@example.com"
USER_DISPLAY_NAME = "Alice"

RECIPE_UUID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

# Production app mounts the recipes router under ``settings.api_v1_prefix``
# (``/api/v1``), so the externally visible URL is ``/api/v1/recipes/...``.
# Issue spec references ``/api/recipes/...`` as shorthand; we use the
# real production paths here so a future prefix change surfaces as a
# test failure instead of a silent regression.
RECIPES_BASE = "/api/v1/recipes"


def _current_user(
    *,
    user_id: uuid.UUID = USER_UUID,
    email: str = USER_EMAIL,
    display_name: str | None = USER_DISPLAY_NAME,
    role: str = "user",
) -> CurrentUser:
    """Build a ``CurrentUser`` matching the dependency override."""
    return CurrentUser(
        id=user_id,
        email=email,
        display_name=display_name,
        role=role,
    )


def _valid_payload(**overrides: Any) -> dict:
    """Baseline valid POST/PUT body; overrides patch individual fields."""
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
    """AsyncSession stand-in matching the existing unit-test pattern.

    The recipes router invokes sync repository functions through
    ``await db.run_sync(lambda s: fn(s, ...))``; we intercept by
    passing a dummy sync-session (``SimpleNamespace()``) to the
    lambda and letting it run — but each test monkeypatches the
    repository function so the lambda never touches real SQL.
    """

    async def run_sync(self, fn):
        return fn(SimpleNamespace())


def _persisted_recipe(
    *,
    owner_id: uuid.UUID,
    payload: dict,
    recipe_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    """Build a Recipe-shaped row matching the input payload + owner.

    SimpleNamespace mirrors the SQLAlchemy ORM attribute surface
    well enough for ``RecipeOut.model_validate(...)`` to project it
    via the ``from_attributes=True`` config.
    """
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
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
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def app_with_user():
    """Register dependency overrides on the global app; clean up after.

    Yields an ``httpx.AsyncClient`` bound to ``app.main.app`` so the
    request goes through the registered exception handlers. The
    overrides install:

    * ``get_current_user`` → returns the fixed test ``CurrentUser``.
    * ``get_db`` → yields a ``_FakeSession`` whose ``run_sync``
      forwards to the lambda the handler provides.

    Tests then monkeypatch the recipes module's repository imports to
    control the lambda's behaviour.
    """
    cu = _current_user()

    async def _override_current_user() -> CurrentUser:
        return cu

    async def _override_db():
        yield _FakeSession()

    app.dependency_overrides[get_current_user] = _override_current_user
    app.dependency_overrides[get_db] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        try:
            yield ac
        finally:
            app.dependency_overrides.clear()


@pytest.fixture
async def app_no_auth():
    """Like ``app_with_user`` but does NOT override auth.

    Used by tests that want to verify the real ``get_current_user``
    dependency (and therefore the 401 path) interacts correctly with
    the envelope handler. ``get_db`` is still overridden so requests
    don't try to reach a real Postgres connection.
    """

    async def _override_db():
        yield _FakeSession()

    app.dependency_overrides[get_db] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        try:
            yield ac
        finally:
            app.dependency_overrides.clear()


def _audit_records(caplog, expected_event: str) -> list[dict]:
    """Decode JSON audit payloads from caplog matching the expected event.

    The audit emitter writes a single JSON document to the recipes
    module's logger. This helper filters out non-JSON / non-matching
    records so the assertion only sees the structured rows we care
    about.
    """
    matches: list[dict] = []
    for record in caplog.records:
        if record.name != "app.api.routes.recipes":
            continue
        msg = record.getMessage()
        try:
            decoded = json.loads(msg)
        except (ValueError, TypeError):
            continue
        if not isinstance(decoded, dict):
            continue
        if decoded.get("event") == expected_event:
            matches.append(decoded)
    return matches


# ── 400 validation envelope (POST + PUT) ─────────────────


class TestValidationEnvelope:
    """Pydantic body validation failures must surface as 400 envelopes."""

    async def test_post_missing_title_returns_400_envelope(
        self, app_with_user
    ):
        """POST with missing ``title`` → 400 with envelope + field_errors."""
        body = _valid_payload()
        body.pop("title")
        resp = await app_with_user.post(RECIPES_BASE, json=body)

        assert resp.status_code == 400, resp.text
        out = resp.json()
        assert out["error"] == "validation_failed"
        assert isinstance(out.get("message"), str) and out["message"]
        assert "field_errors" in out
        assert "title" in out["field_errors"]

    async def test_post_servings_zero_returns_400_envelope(
        self, app_with_user
    ):
        """servings=0 (below ge=1) → 400 with ``servings`` in field_errors."""
        resp = await app_with_user.post(
            RECIPES_BASE, json=_valid_payload(servings=0)
        )
        assert resp.status_code == 400, resp.text
        out = resp.json()
        assert out["error"] == "validation_failed"
        assert "servings" in out["field_errors"]

    async def test_post_extra_field_returns_400_envelope(
        self, app_with_user
    ):
        """``extra='forbid'`` on RecipeCreate → 400 with the unknown field key."""
        body = _valid_payload()
        body["tags"] = ["weeknight"]
        resp = await app_with_user.post(RECIPES_BASE, json=body)
        assert resp.status_code == 400, resp.text
        out = resp.json()
        assert out["error"] == "validation_failed"
        # The extra field is reported under its own key — locks the
        # contract that the SPA can attach this to the right input.
        assert "tags" in out["field_errors"]

    async def test_post_string_prep_time_returns_400_envelope(
        self, app_with_user
    ):
        """strict=True rejects string coercion → 400 with ``prep_time``."""
        resp = await app_with_user.post(
            RECIPES_BASE, json=_valid_payload(prep_time="5")
        )
        assert resp.status_code == 400, resp.text
        out = resp.json()
        assert out["error"] == "validation_failed"
        assert "prep_time" in out["field_errors"]

    async def test_put_missing_field_returns_400_envelope(
        self, app_with_user
    ):
        """PUT body missing a required field → 400 envelope."""
        body = _valid_payload()
        body.pop("description")
        resp = await app_with_user.put(
            f"{RECIPES_BASE}/{RECIPE_UUID}", json=body
        )
        assert resp.status_code == 400, resp.text
        out = resp.json()
        assert out["error"] == "validation_failed"
        assert "description" in out["field_errors"]

    async def test_validation_failure_does_not_have_detail_key(
        self, app_with_user
    ):
        """Envelope replaces FastAPI's default ``{"detail": [...]}`` shape.

        Locks the migration from the old shape — a regression that
        accidentally removed the validation handler would surface as
        a 422 with ``detail`` and break the SPA's error parsing.
        """
        body = _valid_payload()
        body.pop("title")
        resp = await app_with_user.post(RECIPES_BASE, json=body)
        assert resp.status_code == 400
        out = resp.json()
        assert "detail" not in out, (
            f"validation response leaked FastAPI default shape: {out}"
        )


# ── 400 malformed-UUID envelope (path) ───────────────────


class TestInvalidRecipeIdEnvelope:
    """``_validate_recipe_id`` raises 400; envelope wraps it."""

    async def test_get_malformed_uuid_returns_invalid_recipe_id_envelope(
        self, app_with_user
    ):
        """GET /recipes/not-a-uuid → 400 envelope with invalid_recipe_id."""
        resp = await app_with_user.get(f"{RECIPES_BASE}/not-a-uuid")
        assert resp.status_code == 400, resp.text
        out = resp.json()
        assert out["error"] == "invalid_recipe_id"
        assert isinstance(out.get("message"), str) and out["message"]
        # Path-error envelope does NOT carry field_errors — it's an
        # HTTPException reshape, not a body validation failure.
        assert "field_errors" not in out

    async def test_delete_malformed_uuid_returns_400_envelope(
        self, app_with_user
    ):
        """DELETE /recipes/not-a-uuid → 400 envelope."""
        resp = await app_with_user.delete(f"{RECIPES_BASE}/garbage")
        assert resp.status_code == 400
        out = resp.json()
        assert out["error"] == "invalid_recipe_id"

    async def test_put_malformed_uuid_returns_400_envelope(
        self, app_with_user
    ):
        """PUT /recipes/not-a-uuid → 400 envelope."""
        resp = await app_with_user.put(
            f"{RECIPES_BASE}/not-a-uuid", json=_valid_payload()
        )
        assert resp.status_code == 400
        out = resp.json()
        assert out["error"] == "invalid_recipe_id"


# ── 404 recipe_not_found envelope ────────────────────────


class TestRecipeNotFoundEnvelope:
    """HTTPException(404, 'recipe_not_found') reshapes to envelope."""

    async def test_get_absent_returns_404_envelope(
        self, app_with_user, monkeypatch
    ):
        """GET an absent recipe → 404 envelope (NOT default ``detail``)."""

        async def _fake_get(session, *, recipe_id, owner_id):
            return None

        monkeypatch.setattr(
            recipes_module, "get_recipe_for_owner", _fake_get
        )

        resp = await app_with_user.get(f"{RECIPES_BASE}/{RECIPE_UUID}")
        assert resp.status_code == 404, resp.text
        out = resp.json()
        assert out["error"] == "recipe_not_found"
        assert isinstance(out.get("message"), str) and out["message"]
        assert "detail" not in out

    async def test_delete_absent_returns_404_envelope(
        self, app_with_user, monkeypatch
    ):
        """DELETE an absent recipe → 404 envelope."""

        def _fake_delete(session, *, recipe_id, owner_id):
            return False

        monkeypatch.setattr(
            recipes_module, "delete_recipe_for_owner", _fake_delete
        )

        resp = await app_with_user.delete(f"{RECIPES_BASE}/{RECIPE_UUID}")
        assert resp.status_code == 404
        out = resp.json()
        assert out["error"] == "recipe_not_found"

    async def test_put_absent_returns_404_envelope(
        self, app_with_user, monkeypatch
    ):
        """PUT an absent recipe → 404 envelope (existence-leak guard)."""

        def _fake_update(session, *, recipe_id, owner_id, data):
            return None

        monkeypatch.setattr(
            recipes_module, "_update_recipe_for_owner", _fake_update
        )

        resp = await app_with_user.put(
            f"{RECIPES_BASE}/{RECIPE_UUID}", json=_valid_payload()
        )
        assert resp.status_code == 404
        out = resp.json()
        assert out["error"] == "recipe_not_found"


# ── 503 db_unavailable envelope ──────────────────────────


def _operational_error(msg: str = "simulated db readonly") -> OperationalError:
    """Construct an OperationalError matching SQLAlchemy's runtime shape."""
    return OperationalError(statement=msg, params={}, orig=Exception(msg))


class TestDbUnavailableEnvelope:
    """OperationalError anywhere in the route stack → 503 envelope."""

    async def test_post_db_operational_error_returns_503_envelope(
        self, app_with_user, monkeypatch
    ):
        """POST that triggers OperationalError → 503 envelope."""

        async def _fake_ensure(db, *, jwt_claims):
            return USER_UUID

        def _fake_create(session, *, owner_id, data):
            raise _operational_error()

        monkeypatch.setattr(recipes_module, "ensure_local_user", _fake_ensure)
        monkeypatch.setattr(recipes_module, "create_recipe", _fake_create)

        resp = await app_with_user.post(RECIPES_BASE, json=_valid_payload())
        assert resp.status_code == 503, resp.text
        out = resp.json()
        assert out["error"] == "db_unavailable"
        assert isinstance(out.get("message"), str) and out["message"]
        # The driver/SQL message MUST NOT leak through.
        assert "simulated db readonly" not in out["message"]

    async def test_get_db_operational_error_returns_503_envelope(
        self, app_with_user, monkeypatch
    ):
        """GET that triggers OperationalError → 503 envelope."""

        async def _fake_get(session, *, recipe_id, owner_id):
            raise _operational_error()

        monkeypatch.setattr(recipes_module, "get_recipe_for_owner", _fake_get)

        resp = await app_with_user.get(f"{RECIPES_BASE}/{RECIPE_UUID}")
        assert resp.status_code == 503
        out = resp.json()
        assert out["error"] == "db_unavailable"

    async def test_list_db_operational_error_returns_503_envelope(
        self, app_with_user, monkeypatch
    ):
        """GET /recipes/mine that triggers OperationalError → 503 envelope."""

        async def _fake_list(session, *, owner_id):
            raise _operational_error()

        monkeypatch.setattr(
            recipes_module, "list_recipes_for_owner", _fake_list
        )

        resp = await app_with_user.get(f"{RECIPES_BASE}/mine")
        assert resp.status_code == 503
        out = resp.json()
        assert out["error"] == "db_unavailable"

    async def test_delete_db_operational_error_returns_503_envelope(
        self, app_with_user, monkeypatch
    ):
        """DELETE that triggers OperationalError → 503 envelope."""

        def _fake_delete(session, *, recipe_id, owner_id):
            raise _operational_error()

        monkeypatch.setattr(
            recipes_module, "delete_recipe_for_owner", _fake_delete
        )

        resp = await app_with_user.delete(f"{RECIPES_BASE}/{RECIPE_UUID}")
        assert resp.status_code == 503
        out = resp.json()
        assert out["error"] == "db_unavailable"

    async def test_put_db_operational_error_returns_503_envelope(
        self, app_with_user, monkeypatch
    ):
        """PUT that triggers OperationalError → 503 envelope."""

        def _fake_update(session, *, recipe_id, owner_id, data):
            raise _operational_error()

        monkeypatch.setattr(
            recipes_module, "_update_recipe_for_owner", _fake_update
        )

        resp = await app_with_user.put(
            f"{RECIPES_BASE}/{RECIPE_UUID}", json=_valid_payload()
        )
        assert resp.status_code == 503
        out = resp.json()
        assert out["error"] == "db_unavailable"


# ── Audit logging ────────────────────────────────────────


class TestAuditLogging:
    """Successful CRUD must emit ``{event, user_id, recipe_id, ts}`` audit lines."""

    async def test_successful_post_emits_recipe_created_audit(
        self, app_with_user, monkeypatch, caplog
    ):
        """POST → 201 emits a single ``event=recipe_created`` log line."""

        async def _fake_ensure(db, *, jwt_claims):
            return USER_UUID

        def _fake_create(session, *, owner_id, data):
            return _persisted_recipe(
                owner_id=owner_id,
                payload=data.model_dump(),
                recipe_id=RECIPE_UUID,
            )

        monkeypatch.setattr(recipes_module, "ensure_local_user", _fake_ensure)
        monkeypatch.setattr(recipes_module, "create_recipe", _fake_create)

        caplog.set_level(logging.INFO, logger="app.api.routes.recipes")
        resp = await app_with_user.post(RECIPES_BASE, json=_valid_payload())
        assert resp.status_code == 201, resp.text

        events = _audit_records(caplog, "recipe_created")
        assert len(events) == 1, f"expected one audit row, got {events}"
        evt = events[0]
        assert evt["user_id"] == str(USER_UUID)
        assert evt["recipe_id"] == str(RECIPE_UUID)
        assert "ts" in evt and evt["ts"]
        assert "request_id" in evt  # may be None if no middleware

    async def test_successful_put_emits_recipe_updated_audit(
        self, app_with_user, monkeypatch, caplog
    ):
        """PUT → 200 emits a single ``event=recipe_updated`` log line."""

        def _fake_update(session, *, recipe_id, owner_id, data):
            return _persisted_recipe(
                owner_id=owner_id,
                payload=data.model_dump(),
                recipe_id=recipe_id,
            )

        monkeypatch.setattr(
            recipes_module, "_update_recipe_for_owner", _fake_update
        )

        caplog.set_level(logging.INFO, logger="app.api.routes.recipes")
        resp = await app_with_user.put(
            f"{RECIPES_BASE}/{RECIPE_UUID}", json=_valid_payload()
        )
        assert resp.status_code == 200, resp.text

        events = _audit_records(caplog, "recipe_updated")
        assert len(events) == 1
        evt = events[0]
        assert evt["user_id"] == str(USER_UUID)
        assert evt["recipe_id"] == str(RECIPE_UUID)
        assert "ts" in evt and evt["ts"]

    async def test_successful_delete_emits_recipe_deleted_audit(
        self, app_with_user, monkeypatch, caplog
    ):
        """DELETE → 204 emits a single ``event=recipe_deleted`` log line."""

        def _fake_delete(session, *, recipe_id, owner_id):
            return True

        monkeypatch.setattr(
            recipes_module, "delete_recipe_for_owner", _fake_delete
        )

        caplog.set_level(logging.INFO, logger="app.api.routes.recipes")
        resp = await app_with_user.delete(f"{RECIPES_BASE}/{RECIPE_UUID}")
        assert resp.status_code == 204, resp.text

        events = _audit_records(caplog, "recipe_deleted")
        assert len(events) == 1
        evt = events[0]
        assert evt["user_id"] == str(USER_UUID)
        assert evt["recipe_id"] == str(RECIPE_UUID)

    async def test_delete_404_does_not_emit_audit(
        self, app_with_user, monkeypatch, caplog
    ):
        """DELETE that returns 404 MUST NOT emit a ``recipe_deleted`` log.

        The contract: emit AFTER the repo confirms a row was deleted,
        not on the no-row path. Locks against the regression of
        emitting before checking the return value.
        """

        def _fake_delete(session, *, recipe_id, owner_id):
            return False

        monkeypatch.setattr(
            recipes_module, "delete_recipe_for_owner", _fake_delete
        )

        caplog.set_level(logging.INFO, logger="app.api.routes.recipes")
        resp = await app_with_user.delete(f"{RECIPES_BASE}/{RECIPE_UUID}")
        assert resp.status_code == 404

        events = _audit_records(caplog, "recipe_deleted")
        assert events == [], (
            f"DELETE that 404'd MUST NOT emit recipe_deleted, got {events}"
        )

    async def test_validation_failure_does_not_emit_audit(
        self, app_with_user, monkeypatch, caplog
    ):
        """Body-validation failures (400) must NOT emit any audit log.

        The validation rejection short-circuits the handler before the
        repo runs; no mutation actually happened, so no audit row.
        """
        async def _boom_ensure(db, *, jwt_claims):
            raise AssertionError("must not run on validation failure")

        def _boom_create(session, *, owner_id, data):
            raise AssertionError("must not run on validation failure")

        monkeypatch.setattr(recipes_module, "ensure_local_user", _boom_ensure)
        monkeypatch.setattr(recipes_module, "create_recipe", _boom_create)

        caplog.set_level(logging.INFO, logger="app.api.routes.recipes")
        body = _valid_payload()
        body.pop("title")
        resp = await app_with_user.post(RECIPES_BASE, json=body)
        assert resp.status_code == 400

        events = _audit_records(caplog, "recipe_created")
        assert events == [], (
            f"validation failure leaked an audit row: {events}"
        )


# ── Success responses unchanged ──────────────────────────


class TestSuccessResponsesUnchanged:
    """Envelope handlers must NOT touch the body of 2xx responses."""

    async def test_successful_post_returns_recipe_out_not_envelope(
        self, app_with_user, monkeypatch
    ):
        """POST happy-path body is still the full RecipeOut, not the envelope."""

        async def _fake_ensure(db, *, jwt_claims):
            return USER_UUID

        def _fake_create(session, *, owner_id, data):
            return _persisted_recipe(
                owner_id=owner_id,
                payload=data.model_dump(),
                recipe_id=RECIPE_UUID,
            )

        monkeypatch.setattr(recipes_module, "ensure_local_user", _fake_ensure)
        monkeypatch.setattr(recipes_module, "create_recipe", _fake_create)

        resp = await app_with_user.post(RECIPES_BASE, json=_valid_payload())
        assert resp.status_code == 201, resp.text
        out = resp.json()
        # Success body has the recipe fields, NOT the envelope shape.
        assert "error" not in out
        assert "message" not in out
        assert out["id"] == str(RECIPE_UUID)
        assert out["owner_id"] == str(USER_UUID)

    async def test_successful_list_returns_array_not_envelope(
        self, app_with_user, monkeypatch
    ):
        """GET /mine happy-path body is the recipe list, not an envelope."""

        async def _fake_list(session, *, owner_id):
            return []

        monkeypatch.setattr(
            recipes_module, "list_recipes_for_owner", _fake_list
        )

        resp = await app_with_user.get(f"{RECIPES_BASE}/mine")
        assert resp.status_code == 200
        assert resp.json() == []


# ── Pre-existing HTTPException dict-detail unchanged ─────


class TestAuthHTTPExceptionEnvelope:
    """The auth layer already raises ``HTTPException(detail={'error': ...})``.

    Verify the handler preserves the ``error`` key and adds a
    ``message`` rather than mangling the existing shape.
    """

    async def test_unauthenticated_get_returns_envelope_with_error_key(
        self, app_no_auth
    ):
        """Missing Authorization → 401 with envelope (error from auth.py).

        The real ``get_current_user`` raises
        ``HTTPException(401, detail={'error': 'unauthenticated'})``;
        the global handler wraps it with a human-readable message.
        """
        resp = await app_no_auth.get(f"{RECIPES_BASE}/mine")
        assert resp.status_code == 401, resp.text
        out = resp.json()
        assert out["error"] == "unauthenticated"
        assert isinstance(out.get("message"), str) and out["message"]
