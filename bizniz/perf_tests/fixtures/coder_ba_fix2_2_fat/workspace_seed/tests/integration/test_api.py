import os
import uuid
import time
import pytest
import httpx

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

USER_EMAIL = "user@example.com"
USER_PASSWORD = "password"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "password"


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        yield c


def _extract_token(body):
    return (
        body.get("token")
        or body.get("access_token")
        or body.get("accessToken")
    )


def _login(client, email, password):
    r = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, f"login failed for {email}: {r.status_code} {r.text}"
    token = _extract_token(r.json())
    assert token, f"login response missing token: {r.json()}"
    return token


def _signup(client, email, password, display_name=None):
    payload = {"email": email, "password": password}
    if display_name is not None:
        payload["display_name"] = display_name
    r = client.post("/api/v1/auth/signup", json=payload)
    assert r.status_code in (200, 201), f"signup failed: {r.status_code} {r.text}"
    body = r.json()
    token = _extract_token(body)
    assert token, f"signup response missing token: {body}"
    return token, body.get("user") or {}


def _sample_recipe(suffix=""):
    return {
        "title": f"Garlic Butter Pasta{(' ' + suffix) if suffix else ''}",
        "description": "A quick weeknight pasta with garlic, butter, and parmesan.",
        "ingredients": [
            "1 lb spaghetti",
            "4 tbsp butter",
            "4 cloves garlic, minced",
            "1/2 cup grated parmesan",
            "Salt and pepper to taste",
        ],
        "instructions": [
            "Bring a large pot of salted water to a boil.",
            "Cook spaghetti to al dente, reserve 1 cup pasta water.",
            "Melt butter in a skillet over medium heat; add garlic and cook 1 minute.",
            "Toss pasta with butter sauce; add parmesan and a splash of pasta water.",
            "Season with salt and pepper; serve immediately.",
        ],
        "prep_time": 10,
        "cook_time": 15,
        "servings": 4,
    }


@pytest.fixture(scope="module")
def user_token(client):
    return _login(client, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
def user_headers(user_token):
    return {"Authorization": f"Bearer {user_token}"}


@pytest.fixture(scope="module")
def admin_token(client):
    return _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def fresh_cook(client):
    unique = uuid.uuid4().hex[:12]
    email = f"cook-{unique}@example.com"
    password = "Password123!"
    token, user = _signup(client, email, password, display_name=f"Cook {unique}")
    return {"email": email, "password": password, "token": token, "user": user}


@pytest.fixture
def fresh_cook_headers(fresh_cook):
    return {"Authorization": f"Bearer {fresh_cook['token']}"}


def test_health_is_live(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text


def test_contract_user_can_log_in_and_see_self(client, user_headers):
    r = client.get("/api/v1/auth/me", headers=user_headers)
    assert r.status_code == 200, f"/auth/me: {r.status_code} {r.text}"
    body = r.json()
    assert body.get("email", "").lower() == USER_EMAIL.lower(), body
    assert body.get("role") == "user", f"expected role user, got {body.get('role')}"


def test_contract_admin_can_log_in_and_see_admin_role(client, admin_headers):
    r = client.get("/api/v1/auth/me", headers=admin_headers)
    assert r.status_code == 200, f"/auth/me: {r.status_code} {r.text}"
    body = r.json()
    assert body.get("email", "").lower() == ADMIN_EMAIL.lower(), body
    assert body.get("role") == "admin", f"expected role admin, got {body.get('role')}"


def test_login_wrong_password_rejected(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"email": USER_EMAIL, "password": "definitely-not-the-password"},
    )
    assert 400 <= r.status_code < 500, f"expected 4xx, got {r.status_code} {r.text}"
    if r.headers.get("content-type", "").startswith("application/json"):
        body = r.json()
        if isinstance(body, dict):
            assert "token" not in body, f"wrong password surfaced token: {body}"


def test_unauthenticated_cannot_list_my_recipes(client):
    r = client.get("/api/v1/recipes/mine")
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


def test_unauthenticated_cannot_create_recipe(client):
    r = client.post("/api/v1/recipes", json=_sample_recipe())
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


def test_create_recipe_then_read_round_trip(client, fresh_cook_headers):
    payload = _sample_recipe()
    r = client.post("/api/v1/recipes", json=payload, headers=fresh_cook_headers)
    assert r.status_code in (200, 201), f"create failed: {r.status_code} {r.text}"
    created = r.json()

    assert "id" in created and created["id"], created
    assert "owner_id" in created and created["owner_id"], created
    assert created["title"] == payload["title"]
    assert created["description"] == payload["description"]
    assert created["ingredients"] == payload["ingredients"]
    assert created["instructions"] == payload["instructions"]
    assert created["prep_time"] == payload["prep_time"]
    assert created["cook_time"] == payload["cook_time"]
    assert created["servings"] == payload["servings"]
    assert "created_at" in created and created["created_at"]
    assert "updated_at" in created and created["updated_at"]

    recipe_id = created["id"]
    r2 = client.get(f"/api/v1/recipes/{recipe_id}", headers=fresh_cook_headers)
    assert r2.status_code == 200, f"get-by-id failed: {r2.status_code} {r2.text}"
    fetched = r2.json()
    assert fetched["id"] == recipe_id
    assert fetched["title"] == payload["title"]
    assert fetched["ingredients"] == payload["ingredients"]
    assert fetched["instructions"] == payload["instructions"]
    assert fetched["owner_id"] == created["owner_id"]


def test_update_recipe_reflects_in_subsequent_get(client, fresh_cook_headers):
    create = client.post(
        "/api/v1/recipes", json=_sample_recipe("v1"), headers=fresh_cook_headers
    )
    assert create.status_code in (200, 201), create.text
    recipe_id = create.json()["id"]

    updated_payload = _sample_recipe("v2")
    updated_payload["title"] = "Updated Garlic Butter Pasta"
    updated_payload["servings"] = 6
    updated_payload["ingredients"] = updated_payload["ingredients"] + ["1 tbsp olive oil"]
    updated_payload["instructions"] = [
        "Bring water to a boil.",
        "Cook pasta until al dente.",
        "Toss with butter, garlic, parmesan, and olive oil.",
        "Serve hot.",
    ]

    upd = client.put(
        f"/api/v1/recipes/{recipe_id}",
        json=updated_payload,
        headers=fresh_cook_headers,
    )
    assert upd.status_code == 200, f"update failed: {upd.status_code} {upd.text}"
    upd_body = upd.json()
    assert upd_body["id"] == recipe_id
    assert upd_body["title"] == "Updated Garlic Butter Pasta"
    assert upd_body["servings"] == 6
    assert upd_body["instructions"] == updated_payload["instructions"]
    assert upd_body["ingredients"] == updated_payload["ingredients"]

    re_read = client.get(f"/api/v1/recipes/{recipe_id}", headers=fresh_cook_headers)
    assert re_read.status_code == 200, re_read.text
    fetched = re_read.json()
    assert fetched["title"] == "Updated Garlic Butter Pasta"
    assert fetched["servings"] == 6
    assert fetched["ingredients"] == updated_payload["ingredients"]
    assert fetched["instructions"] == updated_payload["instructions"]


def test_delete_recipe_then_get_returns_404(client, fresh_cook_headers):
    create = client.post(
        "/api/v1/recipes", json=_sample_recipe("to-delete"), headers=fresh_cook_headers
    )
    assert create.status_code in (200, 201), create.text
    recipe_id = create.json()["id"]

    delete = client.delete(
        f"/api/v1/recipes/{recipe_id}", headers=fresh_cook_headers
    )
    assert delete.status_code in (200, 204), f"delete failed: {delete.status_code} {delete.text}"

    after = client.get(f"/api/v1/recipes/{recipe_id}", headers=fresh_cook_headers)
    assert after.status_code == 404, f"expected 404 after delete, got {after.status_code} {after.text}"


def test_list_my_recipes_sorted_most_recent_first(client):
    unique = uuid.uuid4().hex[:12]
    email = f"sorter-{unique}@example.com"
    token, _ = _signup(client, email, "Password123!", display_name=f"Sorter {unique}")
    headers = {"Authorization": f"Bearer {token}"}

    initial = client.get("/api/v1/recipes/mine", headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json() == [], f"fresh cook should start with 0 recipes: {initial.json()}"

    titles = []
    created_ids = []
    for i in range(3):
        payload = _sample_recipe(f"#{i}")
        payload["title"] = f"Recipe {i} {unique}"
        titles.append(payload["title"])
        r = client.post("/api/v1/recipes", json=payload, headers=headers)
        assert r.status_code in (200, 201), r.text
        created_ids.append(r.json()["id"])
        time.sleep(1.05)

    lst = client.get("/api/v1/recipes/mine", headers=headers)
    assert lst.status_code == 200, lst.text
    items = lst.json()
    assert isinstance(items, list), f"expected list, got {type(items).__name__}"
    assert len(items) == 3, f"expected 3 recipes, got {len(items)}: {items}"

    returned_titles = [it["title"] for it in items]
    assert set(returned_titles) == set(titles), f"titles mismatch: {returned_titles} vs {titles}"

    assert returned_titles[0] == titles[-1], (
        f"expected most-recently-added first; got order {returned_titles}, "
        f"created order {titles}"
    )

    for it in items:
        assert "id" in it and it["id"]
        assert "owner_id" in it and it["owner_id"]
        assert "created_at" in it and it["created_at"]


def test_ownership_strict_other_user_cannot_read_edit_or_delete(client):
    unique_a = uuid.uuid4().hex[:12]
    unique_b = uuid.uuid4().hex[:12]
    token_a, user_a = _signup(client, f"owner-a-{unique_a}@example.com", "Password123!")
    token_b, user_b = _signup(client, f"owner-b-{unique_b}@example.com", "Password123!")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    create = client.post(
        "/api/v1/recipes", json=_sample_recipe("private"), headers=headers_a
    )
    assert create.status_code in (200, 201), create.text
    recipe_id = create.json()["id"]

    get_b = client.get(f"/api/v1/recipes/{recipe_id}", headers=headers_b)
    assert get_b.status_code in (403, 404), (
        f"user B should not be able to read user A's recipe; got {get_b.status_code} {get_b.text}"
    )

    put_b = client.put(
        f"/api/v1/recipes/{recipe_id}",
        json=_sample_recipe("hijack"),
        headers=headers_b,
    )
    assert put_b.status_code in (403, 404), (
        f"user B should not be able to edit user A's recipe; got {put_b.status_code} {put_b.text}"
    )

    del_b = client.delete(f"/api/v1/recipes/{recipe_id}", headers=headers_b)
    assert del_b.status_code in (403, 404), (
        f"user B should not be able to delete user A's recipe; got {del_b.status_code} {del_b.text}"
    )

    list_b = client.get("/api/v1/recipes/mine", headers=headers_b)
    assert list_b.status_code == 200, list_b.text
    ids_b = [it["id"] for it in list_b.json()]
    assert recipe_id not in ids_b, f"user B's listing leaked user A's recipe id: {ids_b}"

    list_a = client.get("/api/v1/recipes/mine", headers=headers_a)
    assert list_a.status_code == 200, list_a.text
    ids_a = [it["id"] for it in list_a.json()]
    assert recipe_id in ids_a, f"owner A cannot see their own recipe: {ids_a}"


def test_create_recipe_validation_rejects_missing_required_field(client, fresh_cook_headers):
    bad = _sample_recipe()
    bad.pop("title")
    r = client.post("/api/v1/recipes", json=bad, headers=fresh_cook_headers)
    assert r.status_code in (400, 422), (
        f"missing title should be rejected, got {r.status_code} {r.text}"
    )


def test_create_recipe_validation_rejects_empty_ingredients(client, fresh_cook_headers):
    bad = _sample_recipe()
    bad["ingredients"] = []
    r = client.post("/api/v1/recipes", json=bad, headers=fresh_cook_headers)
    assert r.status_code in (400, 422), (
        f"empty ingredients should be rejected, got {r.status_code} {r.text}"
    )


def test_get_nonexistent_recipe_returns_404(client, fresh_cook_headers):
    bogus_id = str(uuid.uuid4())
    r = client.get(f"/api/v1/recipes/{bogus_id}", headers=fresh_cook_headers)
    assert r.status_code == 404, f"expected 404 for unknown id, got {r.status_code} {r.text}"