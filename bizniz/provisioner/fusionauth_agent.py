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

    def create_application(self, app_id: str, name: str = "App",
                           frontend_port: int = 5173) -> dict:
        """Create a FusionAuth application with sensible defaults.

        Mirrors the kickstart configuration: JWT enabled, refresh tokens,
        authorization_code grant, redirect URLs for the frontend.
        """
        return self._request("POST", f"/api/application/{app_id}", {
            "application": {
                "name": name,
                "roles": [
                    {"name": "admin", "isSuperRole": True},
                    {"name": "user", "isDefault": True},
                ],
                "oauthConfiguration": {
                    "authorizedRedirectURLs": [
                        f"http://localhost:{frontend_port}/auth/callback",
                    ],
                    "logoutURL": f"http://localhost:{frontend_port}/logout",
                    "requireRegistration": True,
                    "generateRefreshTokens": True,
                    "enabledGrants": ["authorization_code", "refresh_token"],
                },
                "jwtConfiguration": {
                    "enabled": True,
                    "timeToLiveInSeconds": 3600,
                    "refreshTokenTimeToLiveInMinutes": 43200,
                },
            },
        })

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
    auth_spec=None,  # bizniz.auth.spec.AuthSpec | None
) -> dict:
    """Configure FusionAuth for the project and write AUTH_CONTRACT.md.

    Two materialization paths, picked by whether ``auth_spec`` is provided:

    1. **Spec-driven** (preferred). Architect passes the cumulative
       ``AuthSpec`` accumulated from milestone ``auth_delta`` entries.
       Provisioner renders ``kickstart.json`` from the spec, calls
       ``FusionAuthOrchestrator.materialize(spec)`` to reconcile live
       state, then validates the AuthContract. No LLM calls.

    2. **Legacy / fallback** (no ``auth_spec`` provided). Extracts roles
       from the problem statement via the AI client. Kept for backward
       compatibility with callers that don't yet pass a spec.

    Returns a dict with:
      - roles: list of role dicts
      - tenancy_model: "roles" | "groups" | "tenants"
      - test_users: list of {email, password, roles}
      - contract_path: path to AUTH_CONTRACT.md
      - smoke_passed: bool (live login test result)
      - spec_driven: bool (True if path #1 was taken)
    """
    fa = _FusionAuthClient(fusionauth_url, fusionauth_api_key)
    spec_driven = auth_spec is not None and getattr(auth_spec, "enabled", False)

    if spec_driven:
        # ── Path 1: spec-driven ─────────────────────────────────────
        #
        # Persist intent (spec.json) for the engineer/coder/debugger
        # context loader. We deliberately do NOT overwrite the
        # provisioner-rendered kickstart.json — the provisioner owns
        # the bootstrap config (api_key, application_id, admin user)
        # because those values are what gets written into .env and
        # what the skeleton's JWT validation uses as audience. Writing
        # a second kickstart with a name-derived application_id created
        # a split-brain where materialize talked to one app and the
        # skeleton validated tokens against another. Spec changes flow
        # through orchestrator.materialize() (live FA reconciliation)
        # and through the provisioner's kickstart (regenerated when
        # the project is rebuilt from scratch).
        spec_dir = project_root / "docs" / "auth"
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "spec.json").write_text(auth_spec.model_dump_json(indent=2))
        _log(on_status,
             f"FusionAuth agent: wrote spec.json "
             f"({len(auth_spec.applications)} app(s), "
             f"{len(auth_spec.roles)} role(s), "
             f"{len(auth_spec.test_users)} test user(s))")

        # Build legacy-shaped lists for the AuthContract construction
        # below. The orchestrator will materialize() in a moment; this
        # is just so the contract document reflects the spec.
        roles = [
            {
                "name": r.name,
                "description": r.description,
                "is_default": r.is_default,
            }
            for r in auth_spec.roles
        ]
        test_users = [
            {
                "email": u.email,
                "password": u.password,
                "roles": list(u.role_names),
            }
            for u in auth_spec.test_users
        ]
        if auth_spec.multitenant and auth_spec.groups_enabled:
            tenancy_model = "groups"
        elif auth_spec.multitenant:
            tenancy_model = "tenants"
        else:
            tenancy_model = "roles"

        # Materialize via orchestrator (idempotent reconcile against live FA).
        from bizniz.auth import FusionAuthOrchestrator
        orch_for_materialize = FusionAuthOrchestrator(
            base_url=fusionauth_url,
            api_key=fusionauth_api_key,
            on_status=on_status,
        )

        # FusionAuth reports /api/status healthy as soon as its HTTP
        # server is up, but kickstart processing happens AFTER that —
        # apiKey creation, then application/tenant config, then JWKS
        # propagation. We wait for both readiness signals (API key
        # authenticates AND JWKS has keys) before starting materialize
        # so we don't race kickstart on a slow machine.
        #
        # 5-minute deadline, 5-second polls — generous because FA can
        # take a while on first boot and we'd rather wait than burn a
        # whole engineering pass on a kickstart that hadn't finished.
        if not orch_for_materialize.wait_until_fully_ready(
            deadline_s=300.0, poll_s=5.0, on_status=on_status,
        ):
            _log(on_status,
                 "FusionAuth agent: FA not fully ready after 5min — "
                 "proceeding; materialize/validation will surface "
                 "any actual configuration gaps")

        report = orch_for_materialize.materialize(
            auth_spec, primary_app_id=application_id,
        )
        applied = sum(1 for a in report.actions if a.applied)
        failed = sum(1 for a in report.actions if a.error)
        _log(on_status,
             f"FusionAuth agent: orchestrator.materialize — "
             f"{applied} applied, {failed} failed")
        if failed:
            for a in report.actions:
                if a.error:
                    _log(on_status,
                         f"FusionAuth agent: materialize action FAILED — "
                         f"{a.operation}({a.target}): {a.error}")

        # Smoke test: log in as the first test user via the typed
        # orchestrator. We use orch.get_token() rather than the legacy
        # fa.login(), which swallows error responses into {} and made
        # earlier failures look like silent successes.
        smoke_passed = False
        if auth_spec.test_users:
            first = auth_spec.test_users[0]
            _log(on_status,
                 f"FusionAuth agent: smoke test — logging in as {first.email}...")
            try:
                from bizniz.auth.types import FusionAuthError as _FAError
                token = orch_for_materialize.get_token(
                    application_id, first.email, first.password,
                )
                if token:
                    _log(on_status, "FusionAuth agent: smoke test PASS — got valid JWT")
                    smoke_passed = True
            except _FAError as e:
                _log(on_status, f"FusionAuth agent: smoke test FAIL — {e}")
    else:
        # ── Path 2: legacy AI-extraction (kept for backwards compat) ─
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

        # Step 2 (legacy): Ensure the application exists. The kickstart
        # creates it on first boot, but if the DB was recreated or the
        # kickstart didn't run (e.g. FusionAuth container restarted without
        # a fresh DB), we need to create it ourselves.
        existing_app = fa.get_application(application_id)
        if "_error" in existing_app or "application" not in existing_app:
            _log(on_status, f"FusionAuth agent: application {application_id} not found, creating...")
            create_result = fa.create_application(
                application_id,
                name=project_root.name.replace("_", " ").title(),
                frontend_port=frontend_port,
            )
            if "_error" in create_result:
                _log(on_status, f"FusionAuth agent: failed to create application: {create_result}")
            else:
                _log(on_status, "FusionAuth agent: application created")
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

        # Step 3 (legacy): Create test users (one per role)
        test_users = []
        for role in roles:
            name = role["name"]
            if name == "admin":
                continue  # kickstart already creates admin
            email = f"{name}@example.com"
            password = "TestPass123!"
            result = fa.register_user(
                application_id, email, password,
                first_name=name.capitalize(),
                last_name="TestUser",
                roles=[name],
            )
            if "_error" in result and result["_error"] != 409:
                _log(on_status, f"FusionAuth agent: failed to create test user '{email}': {result}")
            else:
                _log(on_status, f"FusionAuth agent: test user '{email}' ready (role: {name})")
            test_users.append({"email": email, "password": password, "roles": [name]})

        # Step 4 (legacy): Smoke test — login with a test user and validate JWT
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

    # Step 5: Build the typed AuthContract, validate it against
    # live FusionAuth, then write AUTH_CONTRACT.md + JSON sidecar.
    # We fail loudly if validation doesn't pass so downstream
    # tests/engineers don't trust a contract that lies.
    from bizniz.auth import (
        AuthContract,
        ContractRole,
        ContractTestUser,
        ContractEndpoint,
        JwtClaimContract,
        RuntimeContract,
        FusionAuthOrchestrator,
    )

    role_names = [r["name"] for r in roles]
    auth_contract = AuthContract(
        project_name=project_root.name,
        application_id=application_id,
        application_name=project_root.name,
        fusionauth_url=fusionauth_url,
        fusionauth_public_url=fusionauth_url,  # provisioner uses internal URL
        tenancy_model=tenancy_model,
        roles=[
            ContractRole(
                name=r["name"],
                description=r.get("description", ""),
                is_default=bool(r.get("is_default", False)),
            )
            for r in roles
        ],
        test_users=[
            ContractTestUser(
                email=u["email"],
                password=u["password"],
                roles=list(u.get("roles", [])),
            )
            for u in test_users
        ],
        skeleton_endpoints=[
            ContractEndpoint("POST", "/api/v1/auth/register",
                             "Register new user (proxies to FusionAuth)"),
            ContractEndpoint("POST", "/api/v1/auth/login",
                             "Login and receive JWT"),
            ContractEndpoint("POST", "/api/v1/auth/refresh",
                             "Refresh access token"),
            ContractEndpoint("GET", "/api/v1/auth/me",
                             "Current user profile",
                             auth_required=True),
            ContractEndpoint("POST", "/api/v1/auth/verify-email",
                             "Verify email address"),
        ],
        fusionauth_endpoints=[
            ContractEndpoint("POST", "/api/login", "FusionAuth login"),
            ContractEndpoint("POST", "/api/user/registration",
                             "Create user + register on application"),
            ContractEndpoint("GET", "/oauth2/userinfo",
                             "Decode access token, return claims"),
        ],
        runtime=RuntimeContract(
            jwks_url=f"{fusionauth_url}/.well-known/jwks.json",
            issuer=fusionauth_url,
            audience=application_id,
            algorithm="RS256",
            jwt_claims=JwtClaimContract(),
        ),
        frontend_port=frontend_port,
    )

    orch = FusionAuthOrchestrator(
        base_url=fusionauth_url,
        api_key=fusionauth_api_key,
        on_status=on_status,
    )
    validation = auth_contract.validate(orch)
    if validation.ok:
        _log(on_status,
             f"FusionAuth agent: contract validated "
             f"({len(validation.checks)} checks passed)")
    else:
        for check in validation.failed_checks:
            _log(on_status,
                 f"FusionAuth agent: contract check FAILED — "
                 f"{check.name}: {check.detail}")
        _log(on_status,
             f"FusionAuth agent: WROTE INVALID CONTRACT "
             f"({len(validation.failed_checks)} of {len(validation.checks)} "
             f"checks failed). Downstream tests will use this contract — "
             f"investigate FusionAuth state.")

    auth_contract.write_to(project_root)
    contract_path = project_root / "AUTH_CONTRACT.md"
    _log(on_status, f"FusionAuth agent: wrote {contract_path} (+ docs/auth/contract.json)")

    return {
        "roles": roles,
        "tenancy_model": tenancy_model,
        "test_users": test_users,
        "contract_path": str(contract_path),
        "smoke_passed": smoke_passed,
        "spec_driven": spec_driven,
        "validation_ok": validation.ok,
        "validation": validation,           # ContractValidationResult
        "auth_contract": auth_contract,     # the AuthContract object (for debugger)
        "orchestrator": orch,               # for debugger to query FA directly
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
