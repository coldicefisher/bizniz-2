"""Integration test suite for the recipe CRUD API (BA-fix1-1).

Exercises every recipe endpoint end-to-end against the LIVE stack:
real FastAPI backend at ``http://backend:8000``, real FusionAuth at
``http://auth:9011``, and a real Postgres on ``DATABASE_URL``. No
mocks for the DB or FusionAuth — all I/O is real.

Coverage targets every critical scenario flagged by the QE report:

* ``create_recipe`` — happy path, missing/empty/whitespace fields,
  oversize ints/strings, strict int coercion, extra-field rejection,
  unicode round-trip, owner_id immutability, cross-user isolation.
* ``list_my_recipes`` — empty list, newest-first sort, cross-user
  isolation, admin-sees-own-only, query-param ignore.
* ``get_recipe`` — happy path, ``cross_user_returns_404`` (the load-
  bearing security property — body MUST equal the absent-UUID body),
  malformed UUID 400 (NOT 422) with ``error='invalid_recipe_id'``,
  deleted-recipe 404.
* ``update_recipe`` — happy path, ``cross_user_returns_404`` with
  unchanged original, owner_id immutability, full-replacement
  semantics for ingredients.
* ``delete_recipe`` — happy path, ``cross_user_returns_404`` with
  unchanged original, double-delete, idempotent 404.
* ``recipes_table_schema`` — compound index name+columns and FK
  cascade on user removal, against the live database.

Fixture strategy
----------------
* ``user_token`` / ``admin_token`` — module-scoped, login the seeded
  FA users ``user@example.com`` and ``admin@example.com``.
* ``user_b_token`` — module-scoped, registers a fresh per-run user
  via the FA admin API and tears it down at module-exit (the
  ``hardDelete=true`` query param hard-removes the row from FA).
* ``_cleanup_recipes`` — function-scoped autouse, wipes every
  recipe owned by user / admin / user_b BEFORE each test so tests
  don't pollute each other. Using the API itself (GET /mine →
  DELETE per id) means the cleanup respects the same ownership
  contract the suite verifies.
* No mocks. The QE report explicitly required "the BE-006-U7
  422→400 coercion must be verified with a real integration test
  against a running stack" — these tests do that.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Iterator
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


# ─── Live-stack coordinates ──────────────────────────────────────────

BACKEND_URL = "http://backend:8000"
FUSIONAUTH_URL = "http://auth:9011"
APPLICATION_ID = "85a03867-dccf-4882-adde-1a79aeec50df"
RECIPES_BASE = f"{BACKEND_URL}/api/v1/recipes"


USER_EMAIL = "user@example.com"
USER_PASSWORD = "password"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "password"


# ─── FusionAuth helpers ──────────────────────────────────────────────


def _fa_login(email: str, password: str) -> str:
    """Exchange (email, password) for a backend-acceptable JWT."""
    resp = httpx.post(
        f"{FUSIONAUTH_URL}/api/login",
        json={
            "loginId": email,
            "password": password,
            "applicationId": APPLICATION_ID,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def _backend_signup(email: str, password: str, display_name: str) -> tuple[str, str]:
    """Sign up a fresh user via the backend's public signup endpoint.

    Returns ``(token, user_id)``. Going through the backend's
    ``/auth/signup`` (rather than FA's admin API) is required because
    FA's issued JWTs do NOT carry an ``email`` claim — the backend's
    auth-mirror self-heal would then insert ``email=''`` and trip the
    UNIQUE-email constraint after the first such user. The signup
    endpoint pre-populates the local mirror correctly so subsequent
    JWT-only flows resolve the user without needing the email claim.
    """
    resp = httpx.post(
        f"{BACKEND_URL}/api/v1/auth/signup",
        json={
            "email": email,
            "password": password,
            "display_name": display_name,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    body = resp.json()
    return body["token"], body["user"]["id"]


def _fa_delete_user_by_email(email: str) -> None:
    """Hard-delete a FA user by email — used in user_b teardown."""
    api_key = os.environ["FUSIONAUTH_API_KEY"]
    # Look up the user id by email.
    lookup = httpx.get(
        f"{FUSIONAUTH_URL}/api/user?email={email}",
        headers={"Authorization": api_key},
        timeout=15.0,
    )
    if lookup.status_code != 200:
        return
    fa_user_id = lookup.json().get("user", {}).get("id")
    if not fa_user_id:
        return
    httpx.delete(
        f"{FUSIONAUTH_URL}/api/user/{fa_user_id}?hardDelete=true",
        headers={"Authorization": api_key},
        timeout=15.0,
    )


def _jwt_sub(token: str) -> uuid.UUID:
    """Extract the ``sub`` claim from a JWT WITHOUT verifying signature.

    The backend already verifies the signature on every protected route;
    here we just want the canonical owner_id the backend will assign to
    recipes this token creates so the suite can cross-reference.
    """
    import base64
    import json

    payload = token.split(".")[1]
    padded = payload + "=" * (4 - len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    return uuid.UUID(claims["sub"])


# ─── Module-scoped identity fixtures ────────────────────────────────


@pytest.fixture(scope="module")
def user_token() -> str:
    """Seeded ``user@example.com`` JWT — module-scoped for reuse."""
    return _fa_login(USER_EMAIL, USER_PASSWORD)


@pytest.fixture(scope="module")
def admin_token() -> str:
    """Seeded ``admin@example.com`` JWT — module-scoped."""
    return _fa_login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def user_b_token() -> Iterator[str]:
    """Fresh per-run secondary user — signed up via backend, deleted from FA on teardown.

    Unique email keeps parallel runs / re-runs after a crash from
    colliding on the FA "duplicate email" path. ``Password123!`` meets
    FA's default policy (8+ chars, mixed case, digit, symbol). Goes
    through the backend's ``/auth/signup`` rather than FA's admin API
    so the local mirror lands seeded with the correct email — see the
    docstring on :func:`_backend_signup`.
    """
    email = f"user_b_{uuid4().hex}@example.com"
    password = "Password123!"
    token, _user_id = _backend_signup(email, password, "User B")
    try:
        yield token
    finally:
        _fa_delete_user_by_email(email)


@pytest.fixture(scope="module")
def user_id(user_token: str) -> uuid.UUID:
    return _jwt_sub(user_token)


@pytest.fixture(scope="module")
def admin_id(admin_token: str) -> uuid.UUID:
    return _jwt_sub(admin_token)


@pytest.fixture(scope="module")
def user_b_id(user_b_token: str) -> uuid.UUID:
    return _jwt_sub(user_b_token)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ─── Per-test cleanup ────────────────────────────────────────────────


def _delete_all_for(token: str) -> None:
    """Drain every recipe owned by the JWT's subject through the public API.

    Using GET /mine + DELETE per-id (rather than a direct SQL TRUNCATE)
    keeps the cleanup loop honest: if the public API ever regresses
    away from owner-scoping, the cleanup will surface the bug too.
    """
    listing = httpx.get(
        f"{RECIPES_BASE}/mine", headers=_headers(token), timeout=15.0
    )
    if listing.status_code != 200:
        return
    for row in listing.json():
        httpx.delete(
            f"{RECIPES_BASE}/{row['id']}",
            headers=_headers(token),
            timeout=15.0,
        )


@pytest.fixture(autouse=True)
def _cleanup_recipes(
    user_token: str, admin_token: str, user_b_token: str
) -> Iterator[None]:
    """Wipe shared-user recipes before AND after each test."""
    _delete_all_for(user_token)
    _delete_all_for(admin_token)
    _delete_all_for(user_b_token)
    yield
    _delete_all_for(user_token)
    _delete_all_for(admin_token)
    _delete_all_for(user_b_token)


# ─── Payload helpers ─────────────────────────────────────────────────


def make_valid_payload(**overrides: Any) -> dict[str, Any]:
    """Fresh dict each call — never share mutable defaults across tests."""
    payload = {
        "title": "Test Recipe",
        "description": "A delicious test recipe.",
        "ingredients": ["flour", "water", "salt"],
        "instructions": ["Mix.", "Bake."],
        "prep_time": 10,
        "cook_time": 20,
        "servings": 4,
    }
    payload.update(overrides)
    return payload


def _create_recipe(token: str, **overrides: Any) -> dict[str, Any]:
    """Helper: POST a valid recipe and return the response JSON."""
    resp = httpx.post(
        RECIPES_BASE,
        json=make_valid_payload(**overrides),
        headers=_headers(token),
        timeout=15.0,
    )
    assert resp.status_code == 201, (
        f"helper create failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


# ════════════════════════════════════════════════════════════════════
#  create_recipe
# ════════════════════════════════════════════════════════════════════


class TestCreateRecipe:
    """POST /api/v1/recipes — auth, validation, ownership."""

    def test_create_happy_path(self, user_token: str, user_id: uuid.UUID):
        """[critical] Valid POST returns 201 with server-assigned id + timestamps."""
        resp = httpx.post(
            RECIPES_BASE,
            json=make_valid_payload(),
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # id is a real UUID
        recipe_id = uuid.UUID(body["id"])
        # owner_id matches the JWT sub claim (server-derived, not body)
        assert uuid.UUID(body["owner_id"]) == user_id
        # timestamps present
        assert body["created_at"] and body["updated_at"]
        # follow-up GET /mine returns this recipe
        mine = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert mine.status_code == 200
        assert any(r["id"] == str(recipe_id) for r in mine.json())

    def test_create_unauthenticated_401(self, user_token: str):
        """[critical] No Authorization → 401; no row created."""
        resp = httpx.post(
            RECIPES_BASE, json=make_valid_payload(), timeout=15.0
        )
        assert resp.status_code == 401, resp.text
        # And the caller's /mine listing remains empty.
        mine = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert mine.status_code == 200
        assert mine.json() == []

    def test_create_client_supplied_owner_id_ignored(
        self, user_token: str
    ):
        """[critical] body.owner_id with another user → 400 (extra='forbid')."""
        body = make_valid_payload()
        body["owner_id"] = str(uuid4())  # would-be other user
        resp = httpx.post(
            RECIPES_BASE,
            json=body,
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        out = resp.json()
        assert out["error"] == "validation_failed"
        assert "owner_id" in out["field_errors"]

    def test_create_missing_required_field_400(self, user_token: str):
        """[important] Missing ``title`` → 400 with title in field_errors."""
        body = make_valid_payload()
        body.pop("title")
        resp = httpx.post(
            RECIPES_BASE,
            json=body,
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        out = resp.json()
        assert out["error"] == "validation_failed"
        assert "title" in out["field_errors"]

    def test_create_empty_ingredients_400(self, user_token: str):
        """[important] Empty ingredients array → 400; /mine still empty."""
        resp = httpx.post(
            RECIPES_BASE,
            json=make_valid_payload(ingredients=[]),
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        assert "ingredients" in resp.json()["field_errors"]
        # No partial save.
        mine = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert mine.json() == []

    def test_create_whitespace_only_ingredient_400(self, user_token: str):
        """[important] Ingredient ``'   '`` → 400; no row created."""
        resp = httpx.post(
            RECIPES_BASE,
            json=make_valid_payload(ingredients=["   "]),
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        assert "ingredients" in resp.json()["field_errors"]
        mine = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert mine.json() == []

    @pytest.mark.parametrize(
        "field,value",
        [
            ("prep_time", -1),
            ("cook_time", 1441),
            ("servings", 0),
            ("servings", 1001),
        ],
    )
    def test_create_oversize_or_negative_integers_400(
        self, user_token: str, field: str, value: int
    ):
        """[important] Out-of-range int fields → 400 with that field flagged."""
        resp = httpx.post(
            RECIPES_BASE,
            json=make_valid_payload(**{field: value}),
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        assert field in resp.json()["field_errors"]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("title", "x" * 201),
            ("description", "y" * 5001),
        ],
    )
    def test_create_oversize_strings_400(
        self, user_token: str, field: str, value: str
    ):
        """[important] title>200 or description>5000 → 400."""
        resp = httpx.post(
            RECIPES_BASE,
            json=make_valid_payload(**{field: value}),
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        assert field in resp.json()["field_errors"]

    def test_create_integer_strict_no_string_coercion(
        self, user_token: str
    ):
        """[important] prep_time='5' → 400 (strict mode, no int coercion)."""
        resp = httpx.post(
            RECIPES_BASE,
            json=make_valid_payload(prep_time="5"),
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        assert "prep_time" in resp.json()["field_errors"]

    def test_create_extra_field_rejected(self, user_token: str):
        """[important] Body with ``tags`` → 400 (extra='forbid')."""
        body = make_valid_payload()
        body["tags"] = ["weeknight"]
        resp = httpx.post(
            RECIPES_BASE,
            json=body,
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        out = resp.json()
        assert out["error"] == "validation_failed"
        assert "tags" in out["field_errors"]

    def test_create_unicode_preserved(self, user_token: str):
        """[nice-to-have] emoji + RTL chars persist byte-identical."""
        title = "Soupe à l'oignon 🧅 שלום"
        body = make_valid_payload(title=title)
        resp = httpx.post(
            RECIPES_BASE,
            json=body,
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["title"] == title
        # Round-trip via GET.
        rid = resp.json()["id"]
        got = httpx.get(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert got.json()["title"] == title

    def test_create_missing_role_403(self, user_token: str):
        """[critical] No-role token → 403. Skipped — FA seed assigns 'user' default.

        Verified at suite design time: registering a FA user with
        ``roles: []`` against this application still yields a JWT
        carrying ``roles: ['user']`` because the primary application
        has a default role configured. There's no path in the live
        test environment to mint a token without the ``user`` or
        ``admin`` role short of changing FA configuration, so this
        scenario is asserted at the unit-test layer in
        ``app/core/tests/test_auth.py`` instead.
        """
        pytest.skip(
            "FA seed assigns 'user' role by default; "
            "no-role-token path covered by unit tests."
        )


# ════════════════════════════════════════════════════════════════════
#  list_my_recipes
# ════════════════════════════════════════════════════════════════════


class TestListMyRecipes:
    """GET /api/v1/recipes/mine — owner-scoped listing."""

    def test_list_happy_path_empty(self, user_token: str):
        """[critical] Fresh slate → 200 with []."""
        resp = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_list_sorted_desc(self, user_token: str):
        """[critical] Three sequential creates → newest-first order."""
        first = _create_recipe(user_token, title="A")
        time.sleep(0.05)
        second = _create_recipe(user_token, title="B")
        time.sleep(0.05)
        third = _create_recipe(user_token, title="C")
        resp = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 200
        ids = [row["id"] for row in resp.json()]
        # Newest-first means the LAST one created appears first.
        assert ids == [third["id"], second["id"], first["id"]], ids

    def test_list_isolation_between_users(
        self, user_token: str, user_b_token: str
    ):
        """[critical] user A creates → user B sees nothing of their own."""
        _create_recipe(user_token, title="A's recipe")
        resp = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(user_b_token),
            timeout=15.0,
        )
        assert resp.status_code == 200
        assert resp.json() == [], (
            f"user B saw user A's recipe — cross-tenant leak: {resp.json()}"
        )

    def test_list_admin_only_sees_own(
        self, user_token: str, admin_token: str
    ):
        """[critical] Admin sees only their own recipes (no moderation this milestone)."""
        admin_recipe = _create_recipe(admin_token, title="Admin pasta")
        _create_recipe(user_token, title="User pizza")
        resp = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(admin_token),
            timeout=15.0,
        )
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert ids == [admin_recipe["id"]], (
            f"admin saw cross-user recipes: {ids}"
        )

    def test_list_unauthenticated_401(self):
        """[critical] No Authorization → 401."""
        resp = httpx.get(f"{RECIPES_BASE}/mine", timeout=15.0)
        assert resp.status_code == 401, resp.text

    def test_list_query_params_ignored(self, user_token: str):
        """[nice-to-have] ?limit=1 returns the full list, not 400."""
        _create_recipe(user_token, title="One")
        _create_recipe(user_token, title="Two")
        resp = httpx.get(
            f"{RECIPES_BASE}/mine?limit=1",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 2  # not truncated

    def test_list_reflects_create_and_delete(self, user_token: str):
        """[important] Round-trip: create → list (present) → delete → list (absent)."""
        created = _create_recipe(user_token)
        rid = created["id"]
        listing1 = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert rid in [r["id"] for r in listing1.json()]
        d = httpx.delete(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert d.status_code == 204, d.text
        listing2 = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert rid not in [r["id"] for r in listing2.json()]


# ════════════════════════════════════════════════════════════════════
#  get_recipe
# ════════════════════════════════════════════════════════════════════


def _absent_404_body(token: str, base: str = RECIPES_BASE) -> dict[str, Any]:
    """Fetch the canonical 404 envelope for a random absent UUID.

    The cross_user_returns_404 tests assert their 404 body equals this
    one — the existence-leak guard from the QE report.
    """
    resp = httpx.get(
        f"{base}/{uuid4()}",
        headers=_headers(token),
        timeout=15.0,
    )
    assert resp.status_code == 404, resp.text
    return resp.json()


class TestGetRecipe:
    """GET /api/v1/recipes/{id} — owner-scoped fetch."""

    def test_get_happy_path(self, user_token: str):
        """[critical] Owner GET returns 200 with full representation."""
        created = _create_recipe(user_token)
        resp = httpx.get(
            f"{RECIPES_BASE}/{created['id']}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 200, resp.text
        got = resp.json()
        assert got["id"] == created["id"]
        assert got["ingredients"] == ["flour", "water", "salt"]
        assert got["instructions"] == ["Mix.", "Bake."]

    def test_get_cross_user_returns_404(
        self, user_token: str, user_b_token: str
    ):
        """[critical] user B GETs user A's recipe → 404 IDENTICAL to absent-row 404.

        Load-bearing security property from the QE report — without
        this assertion, a cross-tenant data-leak (existence oracle)
        could ship undetected.
        """
        created = _create_recipe(user_token, title="Secret")
        absent_body = _absent_404_body(user_b_token)
        resp = httpx.get(
            f"{RECIPES_BASE}/{created['id']}",
            headers=_headers(user_b_token),
            timeout=15.0,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json() == absent_body, (
            "cross-user 404 body differs from absent-UUID 404 — "
            "existence-leak guard regressed."
        )

    def test_get_not_found_404(self, user_token: str):
        """[critical] Random UUID → 404."""
        resp = httpx.get(
            f"{RECIPES_BASE}/{uuid4()}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"] == "recipe_not_found"

    def test_get_malformed_uuid_400(self, user_token: str):
        """[important] GET /recipes/not-a-uuid → 400 (NOT 422), invalid_recipe_id.

        Locks BE-006-U7 + BE-006-fix1: a malformed UUID surfaces as
        400 with ``error='invalid_recipe_id'``, NOT FastAPI's default
        422 ``{detail: [...]}`` shape. QE flagged this scenario as
        REQUIRING a real integration test, not just a unit test.
        """
        resp = httpx.get(
            f"{RECIPES_BASE}/not-a-uuid",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, (
            f"expected 400, got {resp.status_code}: {resp.text}"
        )
        out = resp.json()
        assert out["error"] == "invalid_recipe_id"

    def test_get_unauthenticated_401(self):
        """[critical] No Authorization → 401."""
        resp = httpx.get(f"{RECIPES_BASE}/{uuid4()}", timeout=15.0)
        assert resp.status_code == 401, resp.text

    def test_get_deleted_recipe_404(self, user_token: str):
        """[important] Create, delete, GET → 404."""
        created = _create_recipe(user_token)
        rid = created["id"]
        d = httpx.delete(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert d.status_code == 204
        resp = httpx.get(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 404, resp.text


# ════════════════════════════════════════════════════════════════════
#  update_recipe
# ════════════════════════════════════════════════════════════════════


class TestUpdateRecipe:
    """PUT /api/v1/recipes/{id} — full-replacement, owner-scoped."""

    def test_update_happy_path(self, user_token: str):
        """[critical] PUT updates title; updated_at >= created_at."""
        created = _create_recipe(user_token)
        rid = created["id"]
        new_payload = make_valid_payload(title="Renamed")
        time.sleep(0.05)  # let server clock advance for updated_at compare
        resp = httpx.put(
            f"{RECIPES_BASE}/{rid}",
            json=new_payload,
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["title"] == "Renamed"
        # Subsequent GET reflects the new title.
        got = httpx.get(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert got.json()["title"] == "Renamed"
        # updated_at advances (>= created_at with tolerance for fast clocks).
        assert body["updated_at"] >= body["created_at"]

    def test_update_cross_user_returns_404(
        self, user_token: str, user_b_token: str
    ):
        """[critical] user B PUTs user A's recipe → 404; A's row UNCHANGED.

        The two halves are both required: 404 prevents the cross-
        tenant write, and the unchanged-original check guards against
        a regression where the WHERE clause silently misfires (e.g.
        UPDATE without the owner_id filter would still return 0 rows
        but might also leak the existence of the row in some shapes).
        """
        created = _create_recipe(user_token, title="Original")
        rid = created["id"]
        absent_body = _absent_404_body(user_b_token, RECIPES_BASE)
        resp = httpx.put(
            f"{RECIPES_BASE}/{rid}",
            json=make_valid_payload(title="HIJACKED"),
            headers=_headers(user_b_token),
            timeout=15.0,
        )
        assert resp.status_code == 404, resp.text
        # Body matches the absent-UUID 404 (existence-leak guard).
        assert resp.json() == absent_body
        # User A's recipe is UNCHANGED — verified via their own GET.
        got = httpx.get(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert got.status_code == 200
        assert got.json()["title"] == "Original"

    def test_update_owner_id_immutable(
        self, user_token: str, user_b_token: str, user_b_id: uuid.UUID,
        user_id: uuid.UUID,
    ):
        """[critical] Body with foreign owner_id → 400 OR ignored; row stays original-owned.

        ``RecipeCreate.model_config['extra']='forbid'`` is the
        documented contract — the spec REQUIRES extra='forbid' so we
        assert the 400 path. The fallback assertion (post-update GET
        still owned by original user) protects against any future
        loosening of the schema.
        """
        created = _create_recipe(user_token, title="Owner test")
        rid = created["id"]
        body = make_valid_payload()
        body["owner_id"] = str(user_b_id)  # try to hand off ownership
        resp = httpx.put(
            f"{RECIPES_BASE}/{rid}",
            json=body,
            headers=_headers(user_token),
            timeout=15.0,
        )
        # Per spec: extra='forbid' → 400 with owner_id in field_errors.
        assert resp.status_code == 400, resp.text
        out = resp.json()
        assert "owner_id" in out["field_errors"]
        # Recipe is still owned by the original user.
        got = httpx.get(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert uuid.UUID(got.json()["owner_id"]) == user_id

    def test_update_not_found_404(self, user_token: str):
        """[important] PUT random UUID → 404."""
        resp = httpx.put(
            f"{RECIPES_BASE}/{uuid4()}",
            json=make_valid_payload(),
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 404, resp.text

    def test_update_invalid_field_400(self, user_token: str):
        """[important] PUT with servings=0 → 400 with field_errors.servings."""
        created = _create_recipe(user_token)
        resp = httpx.put(
            f"{RECIPES_BASE}/{created['id']}",
            json=make_valid_payload(servings=0),
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        assert "servings" in resp.json()["field_errors"]

    def test_update_missing_field_400(self, user_token: str):
        """[important] PUT without description → 400 (full replacement)."""
        created = _create_recipe(user_token)
        body = make_valid_payload()
        body.pop("description")
        resp = httpx.put(
            f"{RECIPES_BASE}/{created['id']}",
            json=body,
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        assert "description" in resp.json()["field_errors"]

    def test_update_ingredients_replaced_not_merged(
        self, user_token: str
    ):
        """[important] PUT with 1 ingredient overwrites the original 3 (NOT merge)."""
        created = _create_recipe(
            user_token, ingredients=["flour", "water", "salt"]
        )
        rid = created["id"]
        resp = httpx.put(
            f"{RECIPES_BASE}/{rid}",
            json=make_valid_payload(ingredients=["only-this"]),
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 200, resp.text
        got = httpx.get(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert got.json()["ingredients"] == ["only-this"]

    def test_update_unauthenticated_401(self, user_token: str):
        """[critical] PUT without Authorization → 401."""
        created = _create_recipe(user_token)
        resp = httpx.put(
            f"{RECIPES_BASE}/{created['id']}",
            json=make_valid_payload(),
            timeout=15.0,
        )
        assert resp.status_code == 401, resp.text


# ════════════════════════════════════════════════════════════════════
#  delete_recipe
# ════════════════════════════════════════════════════════════════════


class TestDeleteRecipe:
    """DELETE /api/v1/recipes/{id} — owner-scoped hard delete."""

    def test_delete_happy_path(self, user_token: str):
        """[critical] DELETE → 204, then GET → 404, and /mine no longer lists it."""
        created = _create_recipe(user_token)
        rid = created["id"]
        d = httpx.delete(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert d.status_code == 204, d.text
        got = httpx.get(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert got.status_code == 404
        mine = httpx.get(
            f"{RECIPES_BASE}/mine",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert rid not in [r["id"] for r in mine.json()]

    def test_delete_cross_user_returns_404(
        self, user_token: str, user_b_token: str
    ):
        """[critical] user B DELETEs user A's recipe → 404, A's row STILL EXISTS.

        Load-bearing security property: cross-tenant DELETE MUST NOT
        succeed. The unchanged-original assertion is the live-DB
        proof that the WHERE-clause owner filter actually fired.
        """
        created = _create_recipe(user_token, title="My recipe")
        rid = created["id"]
        absent_body = _absent_404_body(user_b_token)
        resp = httpx.delete(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_b_token),
            timeout=15.0,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json() == absent_body
        # User A's recipe is still present.
        got = httpx.get(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert got.status_code == 200
        assert got.json()["title"] == "My recipe"

    def test_delete_double_returns_404(self, user_token: str):
        """[important] First DELETE 204; second DELETE on same id → 404."""
        created = _create_recipe(user_token)
        rid = created["id"]
        first = httpx.delete(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert first.status_code == 204
        second = httpx.delete(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert second.status_code == 404, second.text

    def test_delete_not_found_404(self, user_token: str):
        """[important] DELETE random UUID → 404."""
        resp = httpx.delete(
            f"{RECIPES_BASE}/{uuid4()}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 404, resp.text

    def test_delete_unauthenticated_401(self, user_token: str):
        """[critical] DELETE without Authorization → 401; row remains."""
        created = _create_recipe(user_token)
        rid = created["id"]
        resp = httpx.delete(f"{RECIPES_BASE}/{rid}", timeout=15.0)
        assert resp.status_code == 401, resp.text
        got = httpx.get(
            f"{RECIPES_BASE}/{rid}",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert got.status_code == 200

    def test_delete_malformed_uuid_400(self, user_token: str):
        """[nice-to-have] DELETE /recipes/not-a-uuid → 400 invalid_recipe_id."""
        resp = httpx.delete(
            f"{RECIPES_BASE}/not-a-uuid",
            headers=_headers(user_token),
            timeout=15.0,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "invalid_recipe_id"


# ════════════════════════════════════════════════════════════════════
#  recipes_table_schema — live-DB structural checks
# ════════════════════════════════════════════════════════════════════


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set; live-DB schema tests skipped")
    return url


class TestRecipesTableSchema:
    """Verify the live-DB schema matches the contract.

    These tests use SQLAlchemy async (asyncpg) to talk to the same
    Postgres the backend talks to. Each test creates its own engine
    so asyncpg's event-loop pinning behaviour doesn't collide with
    pytest-asyncio's function-scope loops.
    """

    async def test_migration_creates_table_and_index(self) -> None:
        """[important] recipes table + (owner_id, created_at DESC, id DESC) index present."""
        url = _database_url()
        engine = create_async_engine(url, future=True)
        try:
            async with engine.connect() as conn:
                # Compound index by name.
                idx_rows = await conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'recipes'"
                    )
                )
                idx_map = {
                    r.indexname: r.indexdef for r in idx_rows.all()
                }
                assert "ix_recipes_owner_created_id" in idx_map, (
                    f"compound sort index missing; have: {list(idx_map)}"
                )
                idxdef = idx_map["ix_recipes_owner_created_id"]
                assert "owner_id" in idxdef
                assert "created_at" in idxdef
                # DESC ordering on the sort columns.
                assert "DESC" in idxdef.upper()

                # All 11 columns are present.
                col_rows = await conn.execute(
                    text(
                        "SELECT column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_name = 'recipes'"
                    )
                )
                cols = {r.column_name: r.is_nullable for r in col_rows.all()}
                expected = {
                    "id",
                    "owner_id",
                    "title",
                    "description",
                    "ingredients",
                    "instructions",
                    "prep_time",
                    "cook_time",
                    "servings",
                    "created_at",
                    "updated_at",
                }
                missing = expected - set(cols)
                assert not missing, f"missing columns: {missing}"
                # Every expected column is NOT NULL.
                for col in expected:
                    assert cols[col] == "NO", (
                        f"column {col} unexpectedly nullable"
                    )
        finally:
            await engine.dispose()

    async def test_fk_cascade_deletes_recipes(self) -> None:
        """[important] Deleting a users row removes their recipes (ON DELETE CASCADE)."""
        url = _database_url()
        engine = create_async_engine(url, future=True)
        try:
            async with engine.begin() as conn:
                uid = uuid.uuid4()
                # Use a unique email to avoid collisions with seeded users.
                email = f"cascade_{uid.hex}@example.com"
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, role) "
                        "VALUES (:id, :email, 'user')"
                    ),
                    {"id": str(uid), "email": email},
                )
                # JSON values are inlined rather than parameterised because
                # asyncpg's parameter rewriter trips over the ``::jsonb``
                # cast when adjacent to a ``:name`` bind. The values are
                # not user input, so inlining is safe here.
                await conn.execute(
                    text(
                        "INSERT INTO recipes "
                        "(owner_id, title, description, ingredients, "
                        "instructions, prep_time, cook_time, servings) "
                        "VALUES (:owner, 't', 'd', '[\"a\"]'::jsonb, "
                        "'[\"x\"]'::jsonb, 1, 1, 1)"
                    ),
                    {"owner": str(uid)},
                )
                # Verify the row exists.
                pre = await conn.execute(
                    text(
                        "SELECT count(*) AS n FROM recipes "
                        "WHERE owner_id = :owner"
                    ),
                    {"owner": str(uid)},
                )
                assert pre.scalar_one() == 1
                # Delete the user.
                await conn.execute(
                    text("DELETE FROM users WHERE id = :id"),
                    {"id": str(uid)},
                )
                # Recipe should be gone via FK CASCADE.
                post = await conn.execute(
                    text(
                        "SELECT count(*) AS n FROM recipes "
                        "WHERE owner_id = :owner"
                    ),
                    {"owner": str(uid)},
                )
                assert post.scalar_one() == 0, (
                    "FK ON DELETE CASCADE did not remove the recipe"
                )
        finally:
            await engine.dispose()
