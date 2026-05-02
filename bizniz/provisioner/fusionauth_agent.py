"""FusionAuth provisioning agent.

Runs after stack validation, before engineering. Reads the problem
statement to understand what roles/access patterns the app needs,
then configures FusionAuth via its API and writes AUTH_CONTRACT.md
so engineers know exactly how auth works.

The AI makes ONE call to extract roles from natural language. Everything
else is deterministic FusionAuth API calls.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)


def _log(on_status: Optional[Callable[[str], None]], msg: str) -> None:
    if on_status:
        on_status(msg)


# ── AI role extraction ───────────────────────────────────────────────────────

_ROLE_EXTRACTION_PROMPT = """\
Read this problem statement and extract the user roles the application needs.

Problem statement:
{problem_statement}

Return a JSON object with:
- "roles": array of role objects, each with:
  - "name": lowercase role identifier (e.g. "landlord", "tenant", "admin")
  - "description": one-line description of what this role can do
  - "is_default": boolean — true if new users should get this role automatically
- "tenancy_model": one of:
  - "roles" — users share one app, access controlled by roles (most common)
  - "groups" — users belong to organizations/teams that share resources
  - "tenants" — each customer gets fully isolated data (true multi-tenant)
- "tenancy_reason": one sentence explaining why you chose that model

Rules:
- Always include an "admin" role with is_default=false
- The problem statement is written in business language, not technical language.
  "Landlord manages properties" means landlord is a role, not a tenant.
- Default to "roles" unless the problem explicitly describes isolated organizations.
- "Each company has its own workspace/data" → "groups"
- "White-label per customer" or "separate deployment per client" → "tenants"

Respond with valid JSON only.
"""


def _extract_roles(
    client,
    problem_statement: str,
    on_status: Optional[Callable[[str], None]] = None,
) -> dict:
    """One AI call to extract roles and tenancy model from the problem statement."""
    from bizniz.clients.chatgpt.types.response_format import ResponseFormat

    _log(on_status, "FusionAuth agent: extracting roles from problem statement...")

    prompt = _ROLE_EXTRACTION_PROMPT.format(problem_statement=problem_statement)

    text, _, _ = client.get_text(
        messages=[
            {"role": "system", "content": "You extract user roles from business requirements. Respond with JSON only."},
            {"role": "user", "content": prompt},
        ],
        use_message_history=False,
        response_format=ResponseFormat.JSON,
    )

    from bizniz.utils.json import clean_llm_json
    result = json.loads(clean_llm_json(text))

    roles = result.get("roles", [])
    model = result.get("tenancy_model", "roles")
    reason = result.get("tenancy_reason", "")

    _log(on_status, f"FusionAuth agent: {len(roles)} role(s) extracted, tenancy={model}")
    for r in roles:
        _log(on_status, f"  - {r['name']}: {r.get('description', '')}")

    return result


# ── FusionAuth API client ────────────────────────────────────────────────────

class _FusionAuthClient:
    """Minimal FusionAuth API client for provisioning."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, method: str, path: str, body: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status in (200, 201):
                    return json.loads(resp.read().decode())
                return {}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            return {"_error": e.code, "_body": body_text}
        except Exception as e:
            return {"_error": str(e)}

    def get_application(self, app_id: str) -> dict:
        return self._request("GET", f"/api/application/{app_id}")

    def create_role(self, app_id: str, role_name: str, is_default: bool = False, is_super: bool = False) -> dict:
        return self._request("POST", f"/api/application/{app_id}/role", {
            "role": {
                "name": role_name,
                "isDefault": is_default,
                "isSuperRole": is_super,
            },
        })

    def register_user(self, app_id: str, email: str, password: str,
                      first_name: str, last_name: str, roles: List[str]) -> dict:
        return self._request("POST", "/api/user/registration", {
            "user": {
                "email": email,
                "password": password,
                "firstName": first_name,
                "lastName": last_name,
            },
            "registration": {
                "applicationId": app_id,
                "roles": roles,
            },
        })

    def login(self, app_id: str, email: str, password: str) -> dict:
        return self._request("POST", "/api/login", {
            "applicationId": app_id,
            "loginId": email,
            "password": password,
        })


# ── Main provisioning function ───────────────────────────────────────────────

def provision_fusionauth(
    *,
    problem_statement: str,
    project_root: Path,
    fusionauth_url: str,
    fusionauth_api_key: str,
    application_id: str,
    frontend_port: int = 5173,
    ai_client=None,
    on_status: Optional[Callable[[str], None]] = None,
) -> dict:
    """Configure FusionAuth for the project and write AUTH_CONTRACT.md.

    Returns a dict with:
      - roles: list of role dicts
      - tenancy_model: "roles" | "groups" | "tenants"
      - test_users: list of {email, password, roles}
      - contract_path: path to AUTH_CONTRACT.md
    """
    fa = _FusionAuthClient(fusionauth_url, fusionauth_api_key)

    # Step 1: Extract roles from problem statement
    if ai_client:
        role_info = _extract_roles(ai_client, problem_statement, on_status)
    else:
        role_info = {
            "roles": [
                {"name": "admin", "description": "Administrator", "is_default": False},
                {"name": "user", "description": "Regular user", "is_default": True},
            ],
            "tenancy_model": "roles",
            "tenancy_reason": "No AI client provided, defaulting to basic roles.",
        }

    roles = role_info.get("roles", [])
    tenancy_model = role_info.get("tenancy_model", "roles")

    # Step 2: Create roles in FusionAuth
    # The kickstart creates "admin" and "user" — we add any additional roles
    existing_app = fa.get_application(application_id)
    existing_roles = set()
    if "application" in existing_app:
        for r in existing_app["application"].get("roles", []):
            existing_roles.add(r["name"])

    for role in roles:
        name = role["name"]
        if name in existing_roles:
            _log(on_status, f"FusionAuth agent: role '{name}' already exists")
            continue
        result = fa.create_role(
            application_id, name,
            is_default=role.get("is_default", False),
            is_super=(name == "admin"),
        )
        if "_error" in result:
            _log(on_status, f"FusionAuth agent: failed to create role '{name}': {result}")
        else:
            _log(on_status, f"FusionAuth agent: created role '{name}'")

    # Step 3: Create test users (one per role)
    test_users = []
    for role in roles:
        name = role["name"]
        if name == "admin":
            continue  # kickstart already creates admin
        email = f"{name}@test.local"
        password = "TestPass123!"
        result = fa.register_user(
            application_id, email, password,
            first_name=name.capitalize(),
            last_name="TestUser",
            roles=[name],
        )
        if "_error" in result and result["_error"] != 409:  # 409 = already exists
            _log(on_status, f"FusionAuth agent: failed to create test user '{email}': {result}")
        else:
            _log(on_status, f"FusionAuth agent: test user '{email}' ready (role: {name})")
        test_users.append({"email": email, "password": password, "roles": [name]})

    # Step 4: Smoke test — login with a test user and validate JWT
    smoke_passed = False
    if test_users:
        test_user = test_users[0]
        _log(on_status, f"FusionAuth agent: smoke test — logging in as {test_user['email']}...")
        login_result = fa.login(application_id, test_user["email"], test_user["password"])
        if "token" in login_result:
            _log(on_status, "FusionAuth agent: smoke test PASS — got valid JWT")
            smoke_passed = True
        else:
            _log(on_status, f"FusionAuth agent: smoke test FAIL — {login_result}")

    # Step 5: Write AUTH_CONTRACT.md
    role_names = [r["name"] for r in roles]
    contract = _build_contract(
        fusionauth_url=fusionauth_url,
        application_id=application_id,
        roles=roles,
        test_users=test_users,
        tenancy_model=tenancy_model,
        frontend_port=frontend_port,
        smoke_passed=smoke_passed,
    )
    contract_path = project_root / "AUTH_CONTRACT.md"
    contract_path.write_text(contract, encoding="utf-8")
    _log(on_status, f"FusionAuth agent: wrote {contract_path}")

    return {
        "roles": roles,
        "tenancy_model": tenancy_model,
        "test_users": test_users,
        "contract_path": str(contract_path),
        "smoke_passed": smoke_passed,
    }


# ── Contract document ────────────────────────────────────────────────────────

def _build_contract(
    fusionauth_url: str,
    application_id: str,
    roles: List[dict],
    test_users: List[dict],
    tenancy_model: str,
    frontend_port: int,
    smoke_passed: bool,
) -> str:
    role_lines = []
    for r in roles:
        default = " (default)" if r.get("is_default") else ""
        role_lines.append(f"- **{r['name']}**{default}: {r.get('description', '')}")

    user_lines = []
    for u in test_users:
        user_lines.append(f"- `{u['email']}` / `{u['password']}` — roles: {', '.join(u['roles'])}")

    return f"""\
# Auth Contract

Authentication is handled by **FusionAuth**. Do NOT implement your
own login, registration, password hashing, or JWT creation. Use the
skeleton's `get_current_user` and `require_roles` dependencies.

## FusionAuth Configuration

| Setting | Value |
|---|---|
| Internal URL | `{fusionauth_url}` |
| Application ID | `{application_id}` |
| JWKS endpoint | `{fusionauth_url}/.well-known/jwks.json` |
| Tenancy model | {tenancy_model} |
| Smoke test | {"PASS" if smoke_passed else "FAIL"} |

## Roles

{chr(10).join(role_lines)}

## Test Users

{chr(10).join(user_lines) if user_lines else "- `admin@<project>.local` / `ChangeMe123!` — roles: admin (created by kickstart)"}

## Auth Endpoints (provided by skeleton)

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/auth/register` | POST | Register a new user (proxies to FusionAuth) |
| `/api/v1/auth/login` | POST | Login and receive JWT tokens |
| `/api/v1/auth/refresh` | POST | Refresh access token |
| `/api/v1/auth/me` | GET | Get current user profile (requires Bearer token) |
| `/api/v1/auth/verify-email` | POST | Verify email address |
| `/api/v1/auth/forgot-password` | POST | Initiate password reset |
| `/api/v1/auth/reset-password` | POST | Complete password reset |

## Protecting Endpoints

```python
from app.core.auth import get_current_user, require_roles
from app.models.user import User

# Any authenticated user
@router.get("/my-stuff")
async def my_stuff(user: User = Depends(get_current_user)):
    # user.user_id is the FusionAuth user ID
    # Filter data: WHERE owner_id = user.user_id
    ...

# Role-restricted
@router.get("/admin-only", dependencies=[Depends(require_roles("admin"))])
async def admin_only():
    ...
```

## Data Scoping

Auth determines WHO the user is. Data scoping (which records they
can see) is application logic:

```python
# Landlord sees only their properties
properties = await db.execute(
    select(Property).where(Property.owner_id == current_user.user_id)
)

# Tenant sees only their lease
lease = await db.execute(
    select(Lease).where(Lease.tenant_id == current_user.user_id)
)
```

Do NOT use FusionAuth for data-level access control. FusionAuth
manages identity and roles. The application manages data ownership.
"""
