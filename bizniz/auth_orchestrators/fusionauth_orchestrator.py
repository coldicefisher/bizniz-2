"""FusionAuth Orchestrator.

Typed, idempotent operations against a FusionAuth instance. Used by
the provisioner to bootstrap an application's auth setup, by the
integration testers to acquire tokens, and by any pipeline component
that needs to query or mutate FusionAuth state.

Design principles:

- **Idempotent ensure_X operations** for the common path. Calling
  ``ensure_role("landlord")`` is safe whether the role exists or not;
  the result is the same.
- **Typed methods** for everything we use repeatedly. Each method
  is documented, tested, and has predictable error semantics.
- **Escape hatch** (``request()``) for one-off operations we haven't
  wrapped. Promotes to a typed method when used >2 places.
- **Minimal AI** — only the bootstrap step (extract roles from
  problem statement) uses an LLM. Everything else is deterministic
  REST.
- **Soft-fail discipline**: idempotent ensure_X never raises on
  "already exists" (HTTP 409). Real failures (network errors,
  permission errors, malformed input) raise FusionAuthError.

Promoted from the private ``_FusionAuthClient`` in
``bizniz/provisioner/fusionauth_agent.py``. The provisioner remains
the AI-bootstrapping shell that calls this class for the actual
FusionAuth operations.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

import requests

from bizniz.auth_orchestrators.types import (
    ApplicationId,
    FusionAuthError,
    FusionAuthRole,
    FusionAuthState,
    FusionAuthUser,
    ReconcileAction,
    ReconcileReport,
    RoleId,
    UserId,
)


class FusionAuthOrchestrator:
    """Source-of-truth wrapper for FusionAuth API operations.

    Parameters
    ----------
    base_url:
        FusionAuth host URL (e.g. ``http://localhost:9011``).
    api_key:
        API key with sufficient scope for the operations the caller
        will perform. The provisioner owns the kickstart-issued
        master key; runtime app code typically gets a scoped key
        via ``rotate_api_key()``.
    timeout_s:
        Per-request HTTP timeout.
    on_status:
        Optional log callback for human-readable status updates.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float = 30.0,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    # ── Escape hatch ────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        *,
        ok_statuses: Optional[List[int]] = None,
    ) -> dict:
        """Generic FusionAuth API call.

        Use this for one-off operations we haven't wrapped in a typed
        method. Promote to a typed method when you find yourself
        calling the same endpoint in two or more places.

        ``ok_statuses`` lets callers accept a wider range than 2xx
        (e.g. ``[200, 404]`` for "delete; missing is fine").
        """
        url = f"{self.base_url}{path}"
        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        try:
            resp = requests.request(
                method=method.upper(),
                url=url,
                headers=headers,
                json=body,
                timeout=self.timeout_s,
            )
        except requests.RequestException as e:
            raise FusionAuthError(
                f"FusionAuth {method} {path} unreachable: {e}",
                status_code=None,
            ) from e

        ok = ok_statuses if ok_statuses is not None else list(range(200, 300))
        if resp.status_code not in ok:
            raise FusionAuthError(
                f"FusionAuth {method} {path} returned {resp.status_code}",
                status_code=resp.status_code,
                response_body=resp.text[:1000],
            )
        if resp.status_code == 204 or not resp.text:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"_raw": resp.text}

    # ── Application ─────────────────────────────────────────────────

    def get_application(self, app_id: ApplicationId) -> Optional[dict]:
        """Return the application's full record, or None if missing."""
        try:
            return self.request("GET", f"/api/application/{app_id}")
        except FusionAuthError as e:
            if e.status_code == 404:
                return None
            raise

    def ensure_application(
        self,
        app_id: ApplicationId,
        name: str,
        verification_strategy: str = "FormField",
    ) -> ApplicationId:
        """Idempotent: ensure an application with this ID + name exists,
        with login configuration suitable for an SPA frontend.

        Returns the application_id (same as input). Safe to call
        repeatedly; existing applications get their login config
        reconciled to the desired SPA-friendly defaults.
        """
        # SPA-friendly defaults: ``requireAuthentication=false`` so the
        # frontend can call ``POST /api/login`` without an API key.
        # FA defaults this to ``true``, which makes every public login
        # 401 regardless of credentials. v33 lesson: pipeline tests
        # used the API key path and saw login_verified=true; real
        # frontend users hit 401 because they correctly omit the key.
        login_config = {
            "requireAuthentication": False,
            "allowTokenRefresh": True,
            "generateRefreshTokens": True,
        }

        existing = self.get_application(app_id)
        if existing is not None:
            # Idempotent reconcile: PATCH login config so previously-
            # provisioned apps get the SPA defaults without needing a
            # full re-provision. Skip if already matching.
            existing_lc = (
                (existing.get("application") or {}).get("loginConfiguration") or {}
            )
            if existing_lc.get("requireAuthentication") is not False:
                self._log(
                    f"FusionAuth: PATCHing application {name!r} login "
                    f"config to requireAuthentication=false"
                )
                self.request(
                    "PATCH", f"/api/application/{app_id}",
                    body={"application": {"loginConfiguration": login_config}},
                )
            return app_id
        self._log(f"FusionAuth: creating application {name!r} ({app_id})")
        body = {
            "application": {
                "name": name,
                "verificationStrategy": verification_strategy,
                "loginConfiguration": login_config,
            }
        }
        self.request("POST", f"/api/application/{app_id}", body=body)
        return app_id

    # ── Roles ───────────────────────────────────────────────────────

    def list_roles(self, app_id: ApplicationId) -> List[FusionAuthRole]:
        app = self.get_application(app_id)
        if app is None:
            return []
        roles_raw = (app.get("application") or {}).get("roles") or []
        return [
            FusionAuthRole(
                role_id=r["id"],
                name=r["name"],
                description=r.get("description"),
                is_default=bool(r.get("isDefault")),
                is_super_role=bool(r.get("isSuperRole")),
            )
            for r in roles_raw
        ]

    def get_role(self, app_id: ApplicationId, name: str) -> Optional[FusionAuthRole]:
        for r in self.list_roles(app_id):
            if r.name == name:
                return r
        return None

    def ensure_role(
        self,
        app_id: ApplicationId,
        name: str,
        description: Optional[str] = None,
        is_default: bool = False,
        is_super_role: bool = False,
    ) -> RoleId:
        """Idempotent: ensure a role with this name exists on the
        application. Returns the role_id."""
        existing = self.get_role(app_id, name)
        if existing is not None:
            return existing.role_id
        self._log(f"FusionAuth: creating role {name!r} on app {app_id}")
        body = {
            "role": {
                "name": name,
                "description": description or "",
                "isDefault": is_default,
                "isSuperRole": is_super_role,
            }
        }
        result = self.request("POST", f"/api/application/{app_id}/role", body=body)
        return (result.get("role") or {}).get("id", "")

    def delete_role(self, app_id: ApplicationId, role_id: RoleId) -> None:
        """Delete a role. 404 is treated as success (already gone)."""
        self.request(
            "DELETE", f"/api/application/{app_id}/role/{role_id}",
            ok_statuses=[200, 204, 404],
        )

    # ── Users ───────────────────────────────────────────────────────

    def get_user_by_email(self, email: str) -> Optional[dict]:
        try:
            return self.request("GET", f"/api/user?email={email}")
        except FusionAuthError as e:
            if e.status_code == 404:
                return None
            raise

    def ensure_user(
        self,
        app_id: ApplicationId,
        email: str,
        password: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        roles: Optional[List[str]] = None,
        verified: bool = True,
        password_change_required: bool = False,
    ) -> UserId:
        """Idempotent: ensure a user with this email exists on the
        application with the given roles.

        Behavior:
          - If user doesn't exist: create + register on application
            with roles.
          - If user exists but not registered on this app: reset
            password (so a stale prior-run password doesn't silently
            break login), then register + assign roles.
          - If user exists and IS registered on this app: reset
            password + assign any missing roles (additive on roles).

        ``password_change_required`` controls FusionAuth's first-login
        rotation flag. False for test users (they need clean login);
        True for the seeded admin (forces production-deployer rotation).

        Returns user_id.
        """
        roles = roles or []
        existing = self.get_user_by_email(email)
        if existing is None:
            return self._create_and_register(
                app_id=app_id, email=email, password=password,
                first_name=first_name, last_name=last_name,
                roles=roles, verified=verified,
            )
        user = existing.get("user") or {}
        user_id = user.get("id")
        if not user_id:
            raise FusionAuthError(
                f"FusionAuth returned a user record without an id for {email}"
            )

        # User exists from a prior run — reset the password to the
        # spec's value so login is deterministic. Without this, a
        # persistent FusionAuth volume from a prior incomplete run
        # silently breaks login (FA returns 404 on wrong password,
        # which is indistinguishable from "user doesn't exist").
        # PATCH avoids a full PUT that would clobber other fields.
        self._log(f"FusionAuth: resetting password for existing user {email}")
        self.request(
            "PATCH", f"/api/user/{user_id}",
            body={"user": {
                "password": password,
                "passwordChangeRequired": password_change_required,
            }},
        )

        registrations = user.get("registrations") or []
        existing_app_reg = next(
            (r for r in registrations if r.get("applicationId") == app_id),
            None,
        )

        if existing_app_reg is None:
            self._log(f"FusionAuth: registering existing user {email} on app {app_id}")
            self.request(
                "POST", f"/api/user/registration/{user_id}",
                body={"registration": {
                    "applicationId": app_id,
                    "roles": roles,
                }},
            )
            return user_id

        # User registered; ensure all requested roles assigned (additive).
        current_roles = set(existing_app_reg.get("roles") or [])
        target_roles = current_roles | set(roles)
        if target_roles != current_roles:
            self._log(f"FusionAuth: updating roles for {email}: +{target_roles - current_roles}")
            self.request(
                "PUT", f"/api/user/registration/{user_id}",
                body={"registration": {
                    "applicationId": app_id,
                    "roles": sorted(target_roles),
                }},
            )
        return user_id

    def _create_and_register(
        self, app_id, email, password, first_name, last_name, roles, verified,
    ) -> UserId:
        self._log(f"FusionAuth: creating user {email} (roles={roles})")
        body = {
            "user": {
                "email": email,
                "password": password,
                "firstName": first_name,
                "lastName": last_name,
                "verified": verified,
            },
            "registration": {
                "applicationId": app_id,
                "roles": roles,
            },
        }
        result = self.request("POST", "/api/user/registration", body=body)
        return (result.get("user") or {}).get("id", "")

    def assign_role(self, app_id: ApplicationId, user_id: UserId, role_name: str) -> None:
        """Add a role to a user's registration. Idempotent."""
        # Read current roles, add the new one if missing.
        user_resp = self.request("GET", f"/api/user/{user_id}")
        user = user_resp.get("user") or {}
        registrations = user.get("registrations") or []
        reg = next(
            (r for r in registrations if r.get("applicationId") == app_id),
            None,
        )
        if reg is None:
            raise FusionAuthError(
                f"User {user_id} is not registered on application {app_id}"
            )
        current_roles = set(reg.get("roles") or [])
        if role_name in current_roles:
            return
        new_roles = sorted(current_roles | {role_name})
        self.request(
            "PUT", f"/api/user/registration/{user_id}",
            body={"registration": {
                "applicationId": app_id,
                "roles": new_roles,
            }},
        )

    def unassign_role(self, app_id: ApplicationId, user_id: UserId, role_name: str) -> None:
        """Remove a role from a user. Idempotent — missing role
        is not an error."""
        user_resp = self.request("GET", f"/api/user/{user_id}")
        user = user_resp.get("user") or {}
        registrations = user.get("registrations") or []
        reg = next(
            (r for r in registrations if r.get("applicationId") == app_id),
            None,
        )
        if reg is None:
            return
        current_roles = set(reg.get("roles") or [])
        if role_name not in current_roles:
            return
        new_roles = sorted(current_roles - {role_name})
        self.request(
            "PUT", f"/api/user/registration/{user_id}",
            body={"registration": {
                "applicationId": app_id,
                "roles": new_roles,
            }},
        )

    def delete_user(self, user_id: UserId) -> None:
        """Hard-delete a user. 404 is success (already gone)."""
        self.request(
            "DELETE", f"/api/user/{user_id}",
            ok_statuses=[200, 204, 404],
        )

    def suspend_user(self, user_id: UserId) -> None:
        """Deactivate a user. They can no longer log in."""
        self.request(
            "PATCH", f"/api/user/{user_id}",
            body={"user": {"active": False}},
        )

    def reactivate_user(self, user_id: UserId) -> None:
        """Re-enable a suspended user."""
        self.request(
            "PATCH", f"/api/user/{user_id}",
            body={"user": {"active": True}},
        )

    # ── Groups ──────────────────────────────────────────────────────

    def get_group_by_name(self, name: str) -> Optional[dict]:
        """Find a group by name. Returns the raw group dict or None."""
        try:
            result = self.request("GET", "/api/group")
        except FusionAuthError as e:
            if e.status_code in (401, 403):
                raise
            return None
        for g in (result.get("groups") or []):
            if g.get("name") == name:
                return g
        return None

    def ensure_group(
        self,
        name: str,
        description: str = "",
        application_id: Optional[ApplicationId] = None,
        role_names: Optional[List[str]] = None,
    ) -> str:
        """Idempotent: ensure a group exists with the given role grants.

        Returns the group_id. If ``application_id`` is set, the named
        roles are looked up on that application and granted to the
        group (so adding a user to the group grants them those roles).
        """
        role_names = role_names or []
        existing = self.get_group_by_name(name)
        if existing is not None:
            return existing.get("id", "")

        role_grants: List[Dict[str, Any]] = []
        if application_id and role_names:
            for rn in role_names:
                role = self.get_role(application_id, rn)
                if role is None:
                    self._log(
                        f"FusionAuth: group {name!r} references unknown role "
                        f"{rn!r} on app {application_id} — skipping grant"
                    )
                    continue
                role_grants.append({
                    "applicationId": application_id,
                    "roleId": role.role_id,
                })

        self._log(f"FusionAuth: creating group {name!r} (roles={role_names})")
        body: Dict[str, Any] = {
            "group": {
                "name": name,
                "description": description,
            },
        }
        if role_grants:
            body["roleIds"] = [g["roleId"] for g in role_grants]
        result = self.request("POST", "/api/group", body=body)
        return (result.get("group") or {}).get("id", "")

    def add_user_to_group(self, user_id: UserId, group_id: str) -> None:
        """Idempotent: add a user to a group. 409 (already member) is
        treated as success."""
        self.request(
            "POST", "/api/group/member",
            body={
                "members": {
                    group_id: [{"userId": user_id}],
                }
            },
            ok_statuses=[200, 201, 202, 409],
        )

    # ── Tenant / signing key / JWKS inspection ──────────────────────
    #
    # These methods expose FusionAuth-side configuration so the FA
    # debugger (and AuthContract.validate) can detect mismatches that
    # surface only as runtime auth failures. The motivating case: the
    # default tenant uses HS256, JWKS exposes no public keys, and any
    # backend doing ``jwt.decode(token, ..., algorithms=["RS256"])``
    # fails with "Signature verification failed" — but every other
    # check (login, claims) passes because they don't need JWKS. The
    # debugger had nothing to inspect FA config with, so it couldn't
    # diagnose. These methods plug that hole.

    def get_tenant(self, tenant_id: str) -> Optional[dict]:
        """Fetch a tenant's full config. Returns None if not found."""
        try:
            return self.request("GET", f"/api/tenant/{tenant_id}")
        except FusionAuthError as e:
            if e.status_code == 404:
                return None
            raise

    def patch_tenant(self, tenant_id: str, patch: dict) -> dict:
        """Apply a partial update to a tenant.

        FusionAuth's tenant PATCH validator behaves inconsistently
        depending on tenant state:
          - On a freshly-bootstrapped tenant, omitting ``name`` returns
            400 ``[blank]tenant.name``; including ``name`` (even
            unchanged) returns 400 ``[duplicate]tenant.name``.
          - On a mature/configured tenant, both forms work.

        We dual-attempt to handle both: try without name first
        (works on mature tenants and avoids the duplicate-name trap
        when it does), then if FA rejects with a name-related error,
        retry including the existing tenant's name.
        """
        # Strip any name the caller passed; we'll add it on the retry
        # path if needed.
        patch_no_name = {k: v for k, v in patch.items() if k != "name"}
        try:
            return self.request(
                "PATCH", f"/api/tenant/{tenant_id}",
                body={"tenant": patch_no_name},
            )
        except FusionAuthError as e:
            if e.status_code == 400 and "tenant.name" in (e.response_body or ""):
                existing = self.get_tenant(tenant_id)
                tenant_name = "Default"
                if existing and existing.get("tenant"):
                    tenant_name = existing["tenant"].get("name", "Default")
                self._log(
                    f"FusionAuth: PATCH tenant {tenant_id} retried with "
                    f"name={tenant_name!r} after FA rejected without it"
                )
                with_name = dict(patch_no_name)
                with_name["name"] = tenant_name
                return self.request(
                    "PATCH", f"/api/tenant/{tenant_id}",
                    body={"tenant": with_name},
                )
            raise

    def get_jwks(self) -> dict:
        """Fetch FA's JWKS. Returns the raw response.

        Empty ``keys`` list usually means the tenant is signing with
        HS256 (no public key) — switch the tenant's
        ``accessTokenKeyId`` to an RS256 key generated via
        ``generate_signing_key()``.
        """
        try:
            resp = requests.get(
                f"{self.base_url}/.well-known/jwks.json",
                timeout=self.timeout_s,
            )
        except requests.RequestException as e:
            raise FusionAuthError(f"JWKS unreachable: {e}") from e
        if resp.status_code != 200:
            raise FusionAuthError(
                f"JWKS returned {resp.status_code}",
                status_code=resp.status_code,
                response_body=resp.text[:500],
            )
        return resp.json() or {}

    def list_signing_keys(self) -> List[dict]:
        """List every key configured in FusionAuth.

        Includes both symmetric (HS256) and asymmetric (RS256, ES256)
        keys. Useful for diagnosing "why is JWKS empty" — if there
        are no RS256 keys, JWKS will be empty by definition.
        """
        try:
            result = self.request("GET", "/api/key")
        except FusionAuthError:
            return []
        return list(result.get("keys") or [])

    def get_signing_key(self, key_id: str) -> Optional[dict]:
        """Fetch a single key by ID."""
        try:
            result = self.request("GET", f"/api/key/{key_id}")
        except FusionAuthError as e:
            if e.status_code == 404:
                return None
            raise
        return (result or {}).get("key")

    def generate_signing_key(
        self,
        key_id: str,
        *,
        algorithm: str = "RS256",
        length: int = 2048,
        name: str = "Access Token Signing Key",
    ) -> str:
        """Idempotent: ensure a key exists at this ID with the given algo.

        FusionAuth's POST ``/api/key/generate/<id>`` 200s on success
        and 409s if a key already exists at that ID. We treat 409 as
        success (the key is there). Returns the key_id.
        """
        existing = self.get_signing_key(key_id)
        if existing is not None:
            return key_id
        self._log(
            f"FusionAuth: generating {algorithm} key {name!r} ({length} bits) at {key_id}"
        )
        self.request(
            "POST", f"/api/key/generate/{key_id}",
            body={"key": {
                "algorithm": algorithm,
                "name": name,
                "length": length,
            }},
            ok_statuses=[200, 201, 409],
        )
        return key_id

    def set_tenant_signing_key(
        self,
        tenant_id: str,
        key_id: str,
        *,
        also_id_token: bool = True,
    ) -> None:
        """Bind ``key_id`` as the tenant's accessTokenSigningKey.

        DEPRECATED in favor of ``set_application_signing_key`` —
        FusionAuth's tenant PATCH validator on a fresh tenant rejects
        both forms of the body (without name → [blank], with name →
        [duplicate]). Application-level JWT config overrides tenant-
        level and works around the broken validator entirely. Kept
        for callers that genuinely need to set tenant defaults; they
        accept the risk of the FA validator quirk.
        """
        jwt_config = {"accessTokenKeyId": key_id}
        if also_id_token:
            jwt_config["idTokenKeyId"] = key_id

        self._log(
            f"FusionAuth: binding key {key_id} as tenant {tenant_id} "
            f"accessTokenKeyId{' + idTokenKeyId' if also_id_token else ''}"
        )
        self.patch_tenant(tenant_id, {"jwtConfiguration": jwt_config})

    def set_application_signing_key(
        self,
        app_id: ApplicationId,
        key_id: str,
        *,
        also_id_token: bool = True,
    ) -> None:
        """Bind ``key_id`` as the application's accessTokenSigningKey.

        Application-level JWT config overrides the tenant's. Setting
        ``jwtConfiguration.enabled = true`` is required — without it,
        the application falls back to tenant defaults (typically
        HS256 → empty JWKS → backend signature verification fails).

        This is the path FusionAuthDebugger uses for the
        ``jwks_has_keys`` typed-fix because tenant PATCH has a
        contradictory validator on freshly-bootstrapped tenants:
          - PATCH tenant without name → 400 [blank]tenant.name
          - PATCH tenant with same name → 400 [duplicate]tenant.name
        Application PATCH has no such quirk and is the recommended
        path for per-app token customization regardless.
        """
        jwt_config = {
            "enabled": True,
            "accessTokenKeyId": key_id,
        }
        if also_id_token:
            jwt_config["idTokenKeyId"] = key_id

        self._log(
            f"FusionAuth: binding key {key_id} as application {app_id} "
            f"accessTokenKeyId{' + idTokenKeyId' if also_id_token else ''}"
        )
        self.request(
            "PATCH", f"/api/application/{app_id}",
            body={"application": {"jwtConfiguration": jwt_config}},
        )

    def diagnose_jwt_setup(
        self,
        *,
        tenant_id: str,
        app_id: ApplicationId,
        test_email: Optional[str] = None,
        test_password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """End-to-end check of the RS256 + JWKS path.

        Returns a structured report covering:
          - JWKS reachability + key count
          - Tenant's configured signing key id + the key's algorithm
          - Whether a real login mints a JWT whose ``kid`` is in JWKS
            (only run when ``test_email``/``test_password`` provided)

        Soft-fails — populates ``errors`` instead of raising. The FA
        debugger reads this report and, when ``ok=False``, runs the
        appropriate typed fix.
        """
        report: Dict[str, Any] = {
            "ok": True,
            "errors": [],
            "jwks_keys": 0,
            "jwks_kids": [],
            "tenant_access_token_key_id": None,
            "signing_key_algorithm": None,
            "login_token_kid": None,
            "kid_in_jwks": None,
        }

        # 1. JWKS
        try:
            jwks = self.get_jwks()
            keys = jwks.get("keys") or []
            report["jwks_keys"] = len(keys)
            report["jwks_kids"] = [k.get("kid") for k in keys if k.get("kid")]
            if not keys:
                report["ok"] = False
                report["errors"].append(
                    "JWKS endpoint returned 0 keys — tenant likely uses HS256. "
                    "Generate an RS256 key and bind it via set_tenant_signing_key()."
                )
        except FusionAuthError as e:
            report["ok"] = False
            report["errors"].append(f"JWKS fetch failed: {e}")

        # 2. Tenant config
        tenant = self.get_tenant(tenant_id)
        if tenant and tenant.get("tenant"):
            jwt_config = tenant["tenant"].get("jwtConfiguration") or {}
            key_id = jwt_config.get("accessTokenKeyId")
            report["tenant_access_token_key_id"] = key_id
            if key_id:
                key = self.get_signing_key(key_id)
                if key:
                    report["signing_key_algorithm"] = key.get("algorithm")
                else:
                    report["ok"] = False
                    report["errors"].append(
                        f"Tenant references signing key {key_id} but key not found"
                    )
        else:
            report["ok"] = False
            report["errors"].append(f"Tenant {tenant_id} not found")

        # 3. Live login round-trip (if credentials provided)
        if test_email and test_password:
            try:
                token = self.get_token(app_id, test_email, test_password)
                import base64 as _b64
                import json as _json
                parts = token.split(".")
                if len(parts) != 3:
                    raise ValueError("not a JWT")
                hdr_b64 = parts[0] + "=" * (-len(parts[0]) % 4)
                hdr = _json.loads(_b64.urlsafe_b64decode(hdr_b64.encode()))
                kid = hdr.get("kid")
                report["login_token_kid"] = kid
                report["kid_in_jwks"] = kid in (report["jwks_kids"] or [])
                if kid and not report["kid_in_jwks"]:
                    report["ok"] = False
                    report["errors"].append(
                        f"Login token signed with kid={kid!r} which is "
                        f"not in JWKS {report['jwks_kids']!r}"
                    )
            except Exception as e:
                report["ok"] = False
                report["errors"].append(
                    f"Diagnostic login failed: {type(e).__name__}: {e}"
                )

        return report

    # ── Auth ────────────────────────────────────────────────────────

    def get_token(self, app_id: ApplicationId, email: str, password: str) -> str:
        """Log in as a user. Returns the access token (string).

        Raises FusionAuthError on bad credentials or account
        deactivation.

        Uses the admin path (Authorization: <api_key>) — bypasses
        ``loginConfiguration.requireAuthentication`` and rate
        limiting. Suitable for orchestrator-side flows where the
        provisioner needs a token for follow-on calls. For verifying
        that the public ``/api/login`` flow works (what the SPA
        frontend would do), use ``public_login_succeeds`` instead.
        """
        result = self.request(
            "POST", "/api/login",
            body={
                "applicationId": app_id,
                "loginId": email,
                "password": password,
            },
        )
        token = result.get("token")
        if not token:
            raise FusionAuthError(
                f"FusionAuth login for {email} returned no token; "
                f"keys present: {list(result.keys())}",
            )
        return token

    def public_login_succeeds(
        self, app_id: ApplicationId, email: str, password: str,
    ) -> bool:
        """Verify the PUBLIC login flow works — no API key, no admin
        bypass. Mirrors exactly what the SPA frontend does.

        Returns True iff FusionAuth returns 200 + a token to a
        ``POST /api/login`` call WITHOUT the ``Authorization`` header.
        Returns False on 4xx/5xx, network errors, or empty-token
        responses. Never raises — the caller's smoke-test wants a
        clean True/False.

        v33 lesson: the admin path ``get_token`` returns 200 even
        when ``loginConfiguration.requireAuthentication=true`` blocks
        the public flow. The manifest's ``login_verified`` field
        should be grounded in this method, not ``get_token``.
        """
        try:
            resp = requests.request(
                method="POST",
                url=f"{self.base_url}/api/login",
                # CRITICAL: no Authorization header. We're testing
                # the same flow the frontend uses.
                headers={"Content-Type": "application/json"},
                json={
                    "applicationId": app_id,
                    "loginId": email,
                    "password": password,
                },
                timeout=self.timeout_s,
            )
        except Exception:
            return False
        if resp.status_code != 200:
            return False
        try:
            return bool(resp.json().get("token"))
        except Exception:
            return False

    def get_user_info(self, token: str) -> dict:
        """Decode + introspect an access token via FusionAuth's
        /oauth2/userinfo endpoint."""
        url = f"{self.base_url}/oauth2/userinfo"
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout_s,
            )
        except requests.RequestException as e:
            raise FusionAuthError(f"userinfo unreachable: {e}") from e
        if resp.status_code != 200:
            raise FusionAuthError(
                f"userinfo returned {resp.status_code}",
                status_code=resp.status_code,
                response_body=resp.text[:1000],
            )
        return resp.json()

    # ── State ───────────────────────────────────────────────────────

    def crawl(self, app_id: ApplicationId) -> FusionAuthState:
        """Read the application's current state: roles + users.

        Used as input to ``reconcile()`` to compute a diff against
        the desired state. Also valuable for evolve-mode planning
        ("what auth state already exists for this project") and for
        debugging.
        """
        app = self.get_application(app_id)
        if app is None:
            raise FusionAuthError(f"Application {app_id} not found")
        app_data = app.get("application") or {}
        roles = self.list_roles(app_id)

        # Search all users registered on this app. The search endpoint
        # with applicationId scopes the query.
        users: List[FusionAuthUser] = []
        try:
            search = self.request(
                "POST", "/api/user/search",
                body={
                    "search": {
                        "queryString": f"registrations.applicationId:{app_id}",
                        "numberOfResults": 1000,
                    },
                },
            )
            for u in search.get("users") or []:
                regs = u.get("registrations") or []
                this_app_reg = next(
                    (r for r in regs if r.get("applicationId") == app_id),
                    None,
                )
                role_names = (this_app_reg or {}).get("roles") or []
                users.append(FusionAuthUser(
                    user_id=u["id"],
                    email=u.get("email", ""),
                    roles=list(role_names),
                    first_name=u.get("firstName"),
                    last_name=u.get("lastName"),
                    active=bool(u.get("active", True)),
                    verified=bool(u.get("verified", False)),
                ))
        except FusionAuthError:
            # Best-effort — if search isn't permissioned, return roles only
            pass

        return FusionAuthState(
            application_id=app_id,
            application_name=app_data.get("name", ""),
            roles=roles,
            users=users,
        )

    def reconcile(
        self,
        app_id: ApplicationId,
        target_state: FusionAuthState,
        *,
        delete_unrecognized: bool = False,
    ) -> ReconcileReport:
        """Bring FusionAuth state in line with ``target_state``.

        - Adds missing roles + users.
        - Updates user role assignments to match target.
        - Does NOT delete unrecognized roles/users by default
          (set ``delete_unrecognized=True`` to enable; useful for
          test-environment resets, dangerous for production).

        Idempotent: running twice with the same target should be a
        no-op the second time.
        """
        current = self.crawl(app_id)
        report = ReconcileReport()

        # Roles to add
        current_role_names = {r.name for r in current.roles}
        for r in target_state.roles:
            if r.name not in current_role_names:
                action = ReconcileAction(
                    operation="ensure_role",
                    target=f"role:{r.name}",
                    detail=r.description or "",
                )
                try:
                    self.ensure_role(
                        app_id=app_id, name=r.name,
                        description=r.description,
                        is_default=r.is_default,
                        is_super_role=r.is_super_role,
                    )
                    action.applied = True
                except FusionAuthError as e:
                    action.error = str(e)
                report.actions.append(action)

        if delete_unrecognized:
            target_role_names = {r.name for r in target_state.roles}
            for r in current.roles:
                if r.name not in target_role_names:
                    action = ReconcileAction(
                        operation="delete_role",
                        target=f"role:{r.name}",
                    )
                    try:
                        self.delete_role(app_id, r.role_id)
                        action.applied = True
                    except FusionAuthError as e:
                        action.error = str(e)
                    report.actions.append(action)

        # Users
        current_users_by_email = {u.email: u for u in current.users}
        for tu in target_state.users:
            cu = current_users_by_email.get(tu.email)
            action = ReconcileAction(
                operation="ensure_user",
                target=f"user:{tu.email}",
                detail=f"roles={tu.roles}",
            )
            try:
                self.ensure_user(
                    app_id=app_id,
                    email=tu.email,
                    password="",  # password set via separate flow if new
                    first_name=tu.first_name,
                    last_name=tu.last_name,
                    roles=tu.roles,
                    verified=tu.verified,
                )
                action.applied = True
            except FusionAuthError as e:
                action.error = str(e)
            report.actions.append(action)

        if delete_unrecognized:
            target_emails = {u.email for u in target_state.users}
            for cu in current.users:
                if cu.email not in target_emails:
                    action = ReconcileAction(
                        operation="delete_user",
                        target=f"user:{cu.email}",
                    )
                    try:
                        self.delete_user(cu.user_id)
                        action.applied = True
                    except FusionAuthError as e:
                        action.error = str(e)
                    report.actions.append(action)

        return report

    # ── Materialize from spec ───────────────────────────────────────

    def materialize(
        self,
        spec: "Any",
        *,
        primary_app_id: Optional[ApplicationId] = None,
    ) -> ReconcileReport:
        """Bring FusionAuth into the desired state described by ``spec``.

        ``spec`` is a ``bizniz.auth_orchestrators.spec.AuthSpec`` (typed late to keep
        this module free of a hard import cycle through planner). This
        is the single entrypoint the provisioner calls — it walks the
        spec and ensures each application, role, group, and user.

        Behavior:
          - **Disabled spec** (``spec.enabled is False``): no-op,
            returns empty report. Provisioner should skip the FusionAuth
            container in this case but this is also safe defensively.
          - **Applications**: ensured in spec order. Each app gets all
            spec.roles registered (or the app's explicit role_names if
            set). Roles previously registered on the app stay — additive.
          - **Seeded admin**: always created if not present, registered
            on every spec application with super_admin.
          - **Test users**: created with deterministic email/password,
            registered on every app where they have at least one role
            (direct or via group membership).
          - **Groups**: created when ``spec.groups_enabled`` is true.
            Role grants resolved at materialize time.
          - **Soft-delete**: roles in ``spec.deprecated_roles`` are NOT
            hard-deleted from FusionAuth — the orchestrator logs a
            warning and emits a ``soft_delete_role`` action so a human
            can decide. Production data loss must always be opt-in.

        ``primary_app_id`` is the application ID for "the" app in
        single-app deployments; used for backwards compatibility with
        callers that still pass a single app_id (kickstart, contract
        validation). When the spec has multiple applications, the first
        one is the primary if not specified.
        """
        report = ReconcileReport()

        if not getattr(spec, "enabled", False):
            return report

        from bizniz.auth_orchestrators.kickstart import _deterministic_uuid

        # Build map of name → app_id for cross-references (groups, users).
        # When ``primary_app_id`` is provided (the typical case — provisioner
        # already created an application via its kickstart and passed the
        # ID here), every spec.application maps to that ONE application.
        # Without this, materialize would create a second app per spec name
        # and the skeleton's JWT validation (audience=primary_app_id) would
        # reject every token issued under the spec-derived app.
        # Multi-app projects (publisher-aggregator) need explicit work — for
        # now spec.applications collapses to the single primary app.
        app_id_by_name: Dict[str, ApplicationId] = {}
        for app in spec.applications:
            app_id = primary_app_id or _deterministic_uuid("application", app.name)
            app_id_by_name[app.name] = app_id

            action = ReconcileAction(
                operation="ensure_application",
                target=f"app:{app.name}",
                detail=f"id={app_id}",
            )
            try:
                self.ensure_application(app_id=app_id, name=app.name)
                action.applied = True
            except FusionAuthError as e:
                action.error = str(e)
            report.actions.append(action)

            # Register every spec role on this application (or the
            # explicit subset if app.role_names is set).
            target_role_names = (
                app.role_names if app.role_names
                else [r.name for r in spec.roles]
            )
            for rn in target_role_names:
                role_def = next(
                    (r for r in spec.roles if r.name == rn), None,
                )
                role_action = ReconcileAction(
                    operation="ensure_role",
                    target=f"role:{rn}@{app.name}",
                )
                try:
                    self.ensure_role(
                        app_id=app_id,
                        name=rn,
                        description=role_def.description if role_def else "",
                        is_default=role_def.is_default if role_def else False,
                        is_super_role=role_def.is_super_role if role_def else False,
                    )
                    role_action.applied = True
                except FusionAuthError as e:
                    role_action.error = str(e)
                report.actions.append(role_action)

            # Seeded admin's role (super_admin) — implicit, register too.
            for admin_role in spec.seeded_admin.role_names:
                if admin_role in target_role_names:
                    continue  # already handled above
                ra = ReconcileAction(
                    operation="ensure_role",
                    target=f"role:{admin_role}@{app.name}",
                )
                try:
                    self.ensure_role(
                        app_id=app_id,
                        name=admin_role,
                        description="Platform super admin (seeded)",
                        is_super_role=True,
                    )
                    ra.applied = True
                except FusionAuthError as e:
                    ra.error = str(e)
                report.actions.append(ra)

        primary = primary_app_id or (
            next(iter(app_id_by_name.values()), None)
        )
        if primary is None:
            self._log("FusionAuth: spec has no applications — nothing further to materialize")
            return report

        # Soft-delete deprecated roles (warning only; never destroy data).
        for dep in spec.deprecated_roles:
            sd = ReconcileAction(
                operation="soft_delete_role",
                target=f"role:{dep.name}",
                detail=(
                    f"deprecated_at={dep.deprecated_at} reason={dep.reason!r}"
                ),
                applied=True,
            )
            self._log(
                f"FusionAuth: role {dep.name!r} is DEPRECATED in spec — "
                f"leaving in place. Hard-delete requires human approval."
            )
            report.actions.append(sd)

        # Seeded admin (registered on every spec application).
        admin = spec.seeded_admin
        for app_name, app_id in app_id_by_name.items():
            aa = ReconcileAction(
                operation="ensure_user",
                target=f"user:{admin.email}@{app_name}",
            )
            try:
                self.ensure_user(
                    app_id=app_id,
                    email=admin.email,
                    password=admin.password,
                    first_name=admin.first_name,
                    last_name=admin.last_name,
                    roles=list(admin.role_names),
                    verified=True,
                    password_change_required=admin.password_change_required,
                )
                aa.applied = True
            except FusionAuthError as e:
                aa.error = str(e)
            report.actions.append(aa)

        # Test users — register on each app where they have a role
        # (direct role_names or group_names that grant roles on the app).
        for user in spec.test_users:
            for app in spec.applications:
                app_id = app_id_by_name[app.name]
                granted = set(user.role_names)
                # Pull roles via group memberships
                for gn in user.group_names:
                    grp = next(
                        (g for g in spec.groups if g.name == gn), None,
                    )
                    if grp and (grp.application is None or grp.application == app.name):
                        granted.update(grp.role_names)
                app_role_names = set(
                    app.role_names if app.role_names
                    else [r.name for r in spec.roles]
                )
                # Case-insensitive intersection, preserving the app-side
                # canonical case. Real-world specs sometimes have
                # "landlord" on users vs "Landlord" on app roles; a
                # silent skip here was the root cause of v2 smoke runs
                # producing AUTH_CONTRACT.md with phantom test users.
                granted_lc = {r.lower() for r in granted}
                grants_for_app = sorted(
                    arn for arn in app_role_names if arn.lower() in granted_lc
                )
                if not grants_for_app:
                    # Emit an explicit skip action so callers (AuthAgent,
                    # provisioner) see WHY the user wasn't created.
                    report.actions.append(ReconcileAction(
                        operation="skip_user",
                        target=f"user:{user.email}@{app.name}",
                        detail=(
                            f"no role overlap (case-insensitive) — "
                            f"user.roles={sorted(granted)}, "
                            f"app.roles={sorted(app_role_names)}"
                        ),
                    ))
                    continue

                ua = ReconcileAction(
                    operation="ensure_user",
                    target=f"user:{user.email}@{app.name}",
                    detail=f"roles={grants_for_app}",
                )
                try:
                    self.ensure_user(
                        app_id=app_id,
                        email=user.email,
                        password=user.password,
                        first_name=user.first_name,
                        last_name=user.last_name,
                        roles=grants_for_app,
                        verified=user.verified,
                        password_change_required=user.password_change_required,
                    )
                    ua.applied = True
                except FusionAuthError as e:
                    ua.error = str(e)
                report.actions.append(ua)

        # Groups (only when explicitly enabled in the spec).
        if spec.groups_enabled:
            user_id_cache: Dict[str, UserId] = {}
            for group in spec.groups:
                app_id = (
                    app_id_by_name.get(group.application)
                    if group.application else primary
                )
                ga = ReconcileAction(
                    operation="ensure_group",
                    target=f"group:{group.name}",
                )
                try:
                    group_id = self.ensure_group(
                        name=group.name,
                        description=group.description,
                        application_id=app_id,
                        role_names=group.role_names,
                    )
                    ga.applied = True
                except FusionAuthError as e:
                    ga.error = str(e)
                    report.actions.append(ga)
                    continue
                report.actions.append(ga)

                # Add any test users that reference this group.
                for user in spec.test_users:
                    if group.name not in user.group_names:
                        continue
                    if user.email not in user_id_cache:
                        u = self.get_user_by_email(user.email)
                        if u is None:
                            continue
                        user_id_cache[user.email] = (u.get("user") or {}).get("id", "")
                    uid = user_id_cache[user.email]
                    if not uid:
                        continue
                    ma = ReconcileAction(
                        operation="add_user_to_group",
                        target=f"user:{user.email}→group:{group.name}",
                    )
                    try:
                        self.add_user_to_group(uid, group_id)
                        ma.applied = True
                    except FusionAuthError as e:
                        ma.error = str(e)
                    report.actions.append(ma)

        return report

    # ── API key management ──────────────────────────────────────────

    def rotate_api_key(self, key_id: str) -> str:
        """Rotate (regenerate) an API key. Returns the new key value.

        FusionAuth doesn't expose a direct "rotate" endpoint — we
        delete + recreate. The caller is responsible for re-storing
        the new key in their secrets store.
        """
        # Read the existing key's permissions before deleting
        existing = self.request("GET", f"/api/api-key/{key_id}")
        existing_key = existing.get("apiKey") or {}
        permissions = existing_key.get("permissions")

        self.request(
            "DELETE", f"/api/api-key/{key_id}",
            ok_statuses=[200, 204, 404],
        )
        result = self.request(
            "POST", f"/api/api-key/{key_id}",
            body={"apiKey": {"permissions": permissions} if permissions else {}},
        )
        new_key = (result.get("apiKey") or {}).get("key")
        if not new_key:
            raise FusionAuthError("rotate_api_key: server returned no key value")
        return new_key

    # ── Health ──────────────────────────────────────────────────────

    def wait_until_ready(self, deadline_s: float = 60.0, poll_s: float = 2.0) -> bool:
        """Block until FusionAuth's status endpoint reports ready, or
        the deadline expires. Returns True on ready, False on timeout.

        ``/api/status`` responds 200 as soon as the HTTP server is up,
        which is BEFORE kickstart finishes creating the bootstrap
        apiKey + application. Use ``wait_until_authenticated`` for the
        stronger guarantee that the orchestrator's api_key actually
        works."""
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            try:
                resp = requests.get(
                    f"{self.base_url}/api/status", timeout=5.0,
                )
                if resp.status_code == 200:
                    return True
            except requests.RequestException:
                pass
            time.sleep(poll_s)
        return False

    def wait_until_authenticated(
        self,
        deadline_s: float = 30.0,
        poll_s: float = 1.5,
    ) -> bool:
        """Block until the orchestrator's api_key actually authenticates
        against FusionAuth. Stronger than ``wait_until_ready`` — kickstart
        processes apiKeys after the HTTP server is up, so /api/status
        reports healthy before the key is actually usable. Polls a
        known-good endpoint (``GET /api/application``) with the api_key
        until it returns 200.
        """
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            try:
                resp = requests.get(
                    f"{self.base_url}/api/application",
                    headers={"Authorization": self.api_key},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    return True
            except requests.RequestException:
                pass
            time.sleep(poll_s)
        return False

    def wait_until_jwks_populated(
        self,
        deadline_s: float = 15.0,
        poll_s: float = 1.0,
    ) -> bool:
        """Block until the JWKS endpoint exposes at least one key.

        FusionAuth's JWKS endpoint has a propagation delay after
        application/tenant config changes — even after a successful
        PATCH binding an RS256 key, JWKS may report ``{"keys":[]}``
        for a few seconds before the key becomes visible. The
        contract validator's ``jwks_has_keys`` check fires
        immediately after materialize and was racing this delay.

        Returns True once at least one key appears, False on timeout.
        """
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            try:
                resp = requests.get(
                    f"{self.base_url}/.well-known/jwks.json",
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    body = resp.json() or {}
                    if body.get("keys"):
                        return True
            except requests.RequestException:
                pass
            time.sleep(poll_s)
        return False

    def wait_until_fully_ready(
        self,
        deadline_s: float = 300.0,
        poll_s: float = 5.0,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Block until FusionAuth is fully ready for production work.

        Two conditions must both be true:
          1. The orchestrator's API key authenticates against FA
             (kickstart's apiKeys block has been applied)
          2. JWKS exposes at least one signing key (kickstart's
             application/tenant JWT config has propagated)

        Both conditions are checked on every poll. Returns True the
        first cycle they're both green; False if the deadline expires.
        Default is 5-minute deadline with 5-second polls — generous
        because FA on a slow machine can take 30-60s to finish
        kickstart processing AND another few seconds for JWKS to
        propagate after.

        This consolidates ``wait_until_authenticated`` and
        ``wait_until_jwks_populated`` into one trip — preferred for
        all production-readiness checks. The individual methods
        remain for callers that only care about one signal.
        """
        end = time.monotonic() + deadline_s
        attempt = 0
        while time.monotonic() < end:
            attempt += 1
            api_ok = False
            jwks_ok = False
            try:
                resp = requests.get(
                    f"{self.base_url}/api/application",
                    headers={"Authorization": self.api_key},
                    timeout=5.0,
                )
                api_ok = resp.status_code == 200
            except requests.RequestException:
                pass
            try:
                resp = requests.get(
                    f"{self.base_url}/.well-known/jwks.json",
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    body = resp.json() or {}
                    jwks_ok = bool(body.get("keys"))
            except requests.RequestException:
                pass

            if api_ok and jwks_ok:
                if on_status:
                    elapsed = deadline_s - (end - time.monotonic())
                    on_status(
                        f"FusionAuth: fully ready after {elapsed:.0f}s "
                        f"(attempt {attempt})"
                    )
                return True

            if on_status and attempt % 6 == 1:  # log every ~30s
                missing = []
                if not api_ok:
                    missing.append("api-key")
                if not jwks_ok:
                    missing.append("jwks")
                on_status(
                    f"FusionAuth: not fully ready (attempt {attempt}, "
                    f"missing: {', '.join(missing)})"
                )
            time.sleep(poll_s)

        if on_status:
            on_status(
                f"FusionAuth: NOT fully ready after {deadline_s:.0f}s "
                f"({attempt} attempts) — proceeding anyway"
            )
        return False
