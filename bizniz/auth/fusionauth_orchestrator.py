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

from bizniz.auth.types import (
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
        """Idempotent: ensure an application with this ID + name exists.

        Returns the application_id (same as input). Safe to call
        repeatedly; existing applications are left in place.
        """
        existing = self.get_application(app_id)
        if existing is not None:
            return app_id
        self._log(f"FusionAuth: creating application {name!r} ({app_id})")
        body = {
            "application": {
                "name": name,
                "verificationStrategy": verification_strategy,
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

    # ── Auth ────────────────────────────────────────────────────────

    def get_token(self, app_id: ApplicationId, email: str, password: str) -> str:
        """Log in as a user. Returns the access token (string).

        Raises FusionAuthError on bad credentials or account
        deactivation.
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

        ``spec`` is a ``bizniz.auth.spec.AuthSpec`` (typed late to keep
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

        from bizniz.auth.kickstart import _deterministic_uuid

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
                grants_for_app = sorted(granted & app_role_names)
                if not grants_for_app:
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
        the deadline expires. Returns True on ready, False on timeout."""
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
