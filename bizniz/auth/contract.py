"""AuthContract — typed representation of AUTH_CONTRACT.md.

The AUTH_CONTRACT.md file in a project root is the source of truth
for the AI agents (engineer, coders, testers) about how the project's
auth is configured. Today it's a hand-rendered Markdown string
inside the FusionAuth provisioner. This module replaces that with:

1. A typed dataclass (``AuthContract``) holding every fact the
   AI agents need to know.
2. A renderer (``to_markdown()``) that produces the canonical
   Markdown to land at ``<project>/AUTH_CONTRACT.md``.
3. A JSON sidecar (``to_json()``) at
   ``<project>/docs/auth/contract.json`` for machine-readable
   consumption (other pipeline components, future agents).
4. A validator (``validate()``) that exercises the contract against
   a live FusionAuth via ``FusionAuthOrchestrator``: every claim
   the contract makes must hold or the provisioner fails the
   milestone.

Without validation, AUTH_CONTRACT.md can lie — claim "landlord
exists" when FusionAuth never created the role, or "test users
can log in" when their passwords are wrong. Downstream agents
trust the contract and produce broken tests/code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests


# ── Typed entities ─────────────────────────────────────────────────


@dataclass
class ContractRole:
    name: str
    description: str = ""
    is_default: bool = False


@dataclass
class ContractTestUser:
    email: str
    password: str
    roles: List[str] = field(default_factory=list)


@dataclass
class ContractEndpoint:
    method: str
    path: str
    description: str
    auth_required: bool = False


@dataclass
class JwtClaimContract:
    """Shape of the JWT issued by the auth provider.

    The skeleton's ``get_current_user`` reads claims by name; the
    contract documents which names are authoritative so the engineer
    doesn't guess.
    """
    subject_claim: str = "sub"            # user_id source
    roles_claim: str = "roles"            # role list source
    email_claim: str = "email"
    issuer: str = ""                       # FusionAuth URL
    audience: Optional[str] = None        # application_id usually


@dataclass
class RuntimeContract:
    """How the BACKEND validates JWTs at request time.

    The generated app reads this section to wire up its
    auth-validation middleware. JWKS endpoint is the single source
    of public keys; claim names map JWT fields to backend
    semantics.
    """
    jwks_url: str
    issuer: str
    audience: str
    algorithm: str = "RS256"
    jwt_claims: JwtClaimContract = field(default_factory=JwtClaimContract)


@dataclass
class ValidationCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ContractValidationResult:
    ok: bool
    checks: List[ValidationCheck] = field(default_factory=list)

    @property
    def failed_checks(self) -> List[ValidationCheck]:
        return [c for c in self.checks if not c.passed]

    def message(self) -> str:
        if self.ok:
            return f"AuthContract validated ({len(self.checks)} checks passed)"
        bad = self.failed_checks
        head = f"AuthContract validation FAILED ({len(bad)} of {len(self.checks)} checks):"
        lines = [head] + [f"  ✗ {c.name}: {c.detail}" for c in bad]
        return "\n".join(lines)


# ── The contract itself ────────────────────────────────────────────


@dataclass
class AuthContract:
    """The single source of truth for a project's auth configuration.

    Constructed by the FusionAuth provisioner after creating the
    application + roles + users in FusionAuth. Validated against
    live FusionAuth before being written to disk. AI agents
    consume the rendered Markdown via prompt injection; pipeline
    code consumes the JSON sidecar.
    """
    # Identity
    project_name: str
    application_id: str
    application_name: str
    fusionauth_url: str               # internal URL (docker network)
    fusionauth_public_url: str = ""   # browser-reachable URL (host)

    # Identity model
    tenancy_model: str = "roles"      # "roles" | "tenants" | "tenants+roles"
    roles: List[ContractRole] = field(default_factory=list)
    test_users: List[ContractTestUser] = field(default_factory=list)

    # API surface (skeleton-provided + FusionAuth direct)
    skeleton_endpoints: List[ContractEndpoint] = field(default_factory=list)
    fusionauth_endpoints: List[ContractEndpoint] = field(default_factory=list)

    # Runtime contract — how the BACKEND validates JWTs
    runtime: Optional[RuntimeContract] = None

    # Frontend integration hints
    frontend_port: int = 5173
    frontend_login_route: str = "/login"
    frontend_after_login_route: str = "/"

    # Validation status — set by validate(), serialized to disk
    validated_at: Optional[str] = None
    validation_passed: bool = False

    # ── Validation ────────────────────────────────────────────────

    def validate(self, orchestrator) -> ContractValidationResult:
        """Run every claim this contract makes against live FusionAuth.

        Updates ``self.validated_at`` and ``self.validation_passed``
        in place. Returns the full result for callers that want to
        display individual check details.
        """
        from bizniz.auth.types import FusionAuthError
        checks: List[ValidationCheck] = []

        # 1. Application exists
        try:
            app = orchestrator.get_application(self.application_id)
            if app is None:
                checks.append(ValidationCheck(
                    "application_exists", False,
                    f"FusionAuth has no application with id {self.application_id}",
                ))
            else:
                checks.append(ValidationCheck("application_exists", True))
        except FusionAuthError as e:
            checks.append(ValidationCheck(
                "application_exists", False, f"FusionAuth API error: {e}",
            ))

        # 2. Each role exists
        for role in self.roles:
            try:
                fa_role = orchestrator.get_role(self.application_id, role.name)
                if fa_role is None:
                    checks.append(ValidationCheck(
                        f"role_exists:{role.name}", False,
                        f"role {role.name!r} not found on application",
                    ))
                else:
                    checks.append(ValidationCheck(f"role_exists:{role.name}", True))
            except FusionAuthError as e:
                checks.append(ValidationCheck(
                    f"role_exists:{role.name}", False, str(e),
                ))

        # 3. Each test user exists, can log in, has the stated roles
        for user in self.test_users:
            # 3a. Exists
            try:
                fa_user = orchestrator.get_user_by_email(user.email)
                if fa_user is None:
                    checks.append(ValidationCheck(
                        f"user_exists:{user.email}", False,
                        "user not found in FusionAuth",
                    ))
                    continue
                checks.append(ValidationCheck(f"user_exists:{user.email}", True))
            except FusionAuthError as e:
                checks.append(ValidationCheck(
                    f"user_exists:{user.email}", False, str(e),
                ))
                continue

            # 3b. Can log in with stated password
            try:
                token = orchestrator.get_token(
                    self.application_id, user.email, user.password,
                )
                checks.append(ValidationCheck(f"user_login:{user.email}", True))
            except FusionAuthError as e:
                checks.append(ValidationCheck(
                    f"user_login:{user.email}", False,
                    f"login rejected: {e}",
                ))
                continue  # can't check roles without a token

            # 3c. Token contains stated roles
            try:
                info = orchestrator.get_user_info(token)
                # FusionAuth puts roles on the registration; userinfo
                # may or may not — try a few common locations.
                token_roles = (
                    info.get("roles")
                    or info.get("application_roles")
                    or []
                )
                if not isinstance(token_roles, list):
                    token_roles = [str(token_roles)]
                missing = set(user.roles) - set(token_roles)
                if missing:
                    checks.append(ValidationCheck(
                        f"user_roles:{user.email}", False,
                        f"missing roles in token: {sorted(missing)} "
                        f"(token has: {token_roles})",
                    ))
                else:
                    checks.append(ValidationCheck(
                        f"user_roles:{user.email}", True,
                    ))
            except FusionAuthError as e:
                checks.append(ValidationCheck(
                    f"user_roles:{user.email}", False, str(e),
                ))

        # 4. JWKS reachable (sanity for the runtime contract)
        if self.runtime and self.runtime.jwks_url:
            try:
                r = requests.get(self.runtime.jwks_url, timeout=5.0)
                if r.status_code == 200 and "keys" in (r.json() or {}):
                    checks.append(ValidationCheck("jwks_reachable", True))
                else:
                    checks.append(ValidationCheck(
                        "jwks_reachable", False,
                        f"JWKS endpoint returned {r.status_code}",
                    ))
            except Exception as e:
                checks.append(ValidationCheck(
                    "jwks_reachable", False, str(e),
                ))

        ok = all(c.passed for c in checks)
        self.validated_at = datetime.now(timezone.utc).isoformat()
        self.validation_passed = ok
        return ContractValidationResult(ok=ok, checks=checks)

    # ── Serialization ─────────────────────────────────────────────

    def to_json(self) -> dict:
        from dataclasses import asdict
        return asdict(self)

    def to_markdown(self) -> str:
        return _render_markdown(self)

    def write_to(self, project_root: Path) -> None:
        """Write AUTH_CONTRACT.md (canonical for human + AI prompt
        consumption) and docs/auth/contract.json (machine-readable
        sidecar)."""
        project_root = Path(project_root)
        md_path = project_root / "AUTH_CONTRACT.md"
        md_path.write_text(self.to_markdown(), encoding="utf-8")

        json_dir = project_root / "docs" / "auth"
        json_dir.mkdir(parents=True, exist_ok=True)
        json_path = json_dir / "contract.json"
        json_path.write_text(
            json.dumps(self.to_json(), indent=2, default=str),
            encoding="utf-8",
        )


# ── Markdown rendering ─────────────────────────────────────────────


def _render_markdown(c: AuthContract) -> str:
    role_lines = []
    for r in c.roles:
        default = " (default for new users)" if r.is_default else ""
        role_lines.append(f"- **{r.name}**{default}: {r.description}")

    user_lines = []
    for u in c.test_users:
        roles_part = ", ".join(u.roles) if u.roles else "(no roles)"
        user_lines.append(f"- `{u.email}` / `{u.password}` — roles: {roles_part}")

    skel_endpoint_rows = []
    for e in c.skeleton_endpoints:
        auth_marker = "🔒 " if e.auth_required else ""
        skel_endpoint_rows.append(
            f"| `{e.path}` | {e.method.upper()} | {auth_marker}{e.description} |"
        )

    fa_endpoint_rows = []
    for e in c.fusionauth_endpoints:
        fa_endpoint_rows.append(
            f"| `{e.path}` | {e.method.upper()} | {e.description} |"
        )

    runtime = c.runtime
    runtime_section = ""
    if runtime:
        runtime_section = f"""
## Runtime Auth Contract (HOW the backend validates tokens)

The backend NEVER mints JWTs and NEVER hashes passwords. FusionAuth
issues all tokens. The backend validates them using these settings:

| Setting | Value |
|---|---|
| JWKS endpoint | `{runtime.jwks_url}` |
| Algorithm | `{runtime.algorithm}` |
| Issuer (`iss` claim) | `{runtime.issuer}` |
| Audience (`aud` claim) | `{runtime.audience}` |

JWT claim mapping (read by `app/core/auth.py:get_current_user`):

| Claim | Backend semantic |
|---|---|
| `{runtime.jwt_claims.subject_claim}` | user_id |
| `{runtime.jwt_claims.roles_claim}` | list of role names |
| `{runtime.jwt_claims.email_claim}` | user email |

```python
# In get_current_user (already shipped by skeleton):
payload = jwt.decode(token, key=jwks_key, algorithms=["{runtime.algorithm}"],
                     audience="{runtime.audience}", issuer="{runtime.issuer}")
user.roles = payload["{runtime.jwt_claims.roles_claim}"]
```
"""

    validation_status = ""
    if c.validated_at:
        marker = "✅ PASSED" if c.validation_passed else "❌ FAILED"
        validation_status = (
            f"\n## Validation\n\n"
            f"This contract was validated against live FusionAuth at "
            f"{c.validated_at}. Status: **{marker}**.\n"
        )

    md = f"""\
# Auth Contract — {c.project_name}

Authentication is handled by **FusionAuth**. The skeleton's
``app/core/auth.py`` validates FusionAuth-issued JWTs. The generated
backend MUST NOT mint its own tokens, hash its own passwords, or
maintain its own role table — FusionAuth owns identity end-to-end.

## FusionAuth Configuration

| Setting | Value |
|---|---|
| Internal URL (Docker network) | `{c.fusionauth_url}` |
| Public URL (browser-reachable) | `{c.fusionauth_public_url or c.fusionauth_url}` |
| Application ID | `{c.application_id}` |
| Application Name | {c.application_name} |
| Tenancy model | {c.tenancy_model} |

## Roles

{chr(10).join(role_lines) if role_lines else "(no roles defined)"}

## Test Users

These users are created during FusionAuth provisioning. Integration
tests MUST use these credentials verbatim (do NOT substitute synthetic
emails — that hides bugs in role mapping and validator config).

{chr(10).join(user_lines) if user_lines else "(none)"}

## Auth Endpoints (provided by the skeleton)

| Path | Method | Description |
|---|---|---|
{chr(10).join(skel_endpoint_rows) if skel_endpoint_rows else "| (none documented) | | |"}
{runtime_section}
## FusionAuth API Endpoints (direct)

| Path | Method | Description |
|---|---|---|
{chr(10).join(fa_endpoint_rows) if fa_endpoint_rows else "| (none documented) | | |"}

## Frontend Integration

| Setting | Value |
|---|---|
| Frontend port | {c.frontend_port} |
| Login route | `{c.frontend_login_route}` |
| Post-login redirect | `{c.frontend_after_login_route}` |

The frontend submits credentials to the backend's `/api/v1/auth/login`
endpoint, which proxies to FusionAuth and returns the access token.
The frontend stores the token (sessionStorage / localStorage per
project convention) and includes `Authorization: Bearer <token>` on
subsequent requests.

## Protecting Endpoints

```python
from app.core.auth import get_current_user, require_roles
from app.models.user import User

# Any authenticated user
@router.get("/my-stuff")
async def my_stuff(user: User = Depends(get_current_user)):
    return user.roles  # populated from JWT claims by the skeleton

# Role-restricted
@router.get("/admin-only", dependencies=[Depends(require_roles("admin"))])
async def admin_only():
    ...
```

## Data Scoping

Auth determines WHO the user is. Data scoping (which records they
can see) is application logic, NOT FusionAuth:

```python
# Filter by user_id, not by role
records = db.query(Record).filter(Record.owner_id == user.user_id)
```

Do NOT use FusionAuth for row-level access control. FusionAuth says
"this user has role X." Your app code says "users with role X scoped
to owner_id Y see records they own."
{validation_status}"""
    return md
