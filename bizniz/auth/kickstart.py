"""Render an ``AuthSpec`` to a FusionAuth kickstart.json.

Kickstart is FusionAuth's first-boot bootstrap mechanism: a JSON file
that describes applications, roles, users, and grants. FusionAuth
applies it once on a fresh database (it's idempotent — re-running on a
populated DB is a no-op). We use it as the "fresh install" path; live
environments reconcile via API instead.

Both paths converge on the same end state — that's the determinism
guarantee. This module is a pure function: ``AuthSpec`` in, ``dict``
out, no I/O, no AI. Test it with golden snapshots.

Reference: https://fusionauth.io/docs/v1/tech/installation-guide/kickstart
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from bizniz.auth.spec import AuthSpec, AppSpec, RoleSpec, UserSpec


def _deterministic_uuid(namespace: str, name: str) -> str:
    """Same name → same UUID, every run. Lets kickstart be diffable
    across runs without churn from random IDs.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"bizniz.{namespace}.{name}"))


def _render_role(role: RoleSpec) -> Dict[str, Any]:
    return {
        "id": _deterministic_uuid("role", role.name),
        "name": role.name,
        "description": role.description,
        "isDefault": role.is_default,
        "isSuperRole": role.is_super_role,
    }


def _render_application(spec: AuthSpec, app: AppSpec) -> Dict[str, Any]:
    """Render one FusionAuth application.

    Roles registered on the app: either the explicit ``role_names`` if
    set, or every role in the spec (so every app sees every role by
    default).
    """
    role_names = app.role_names or spec.all_role_names()
    roles_by_name = {r.name: r for r in spec.roles}
    super_admin_name = spec.seeded_admin.role_names[0] if spec.seeded_admin.role_names else None

    rendered_roles: List[Dict[str, Any]] = []
    for name in role_names:
        if name in roles_by_name:
            rendered_roles.append(_render_role(roles_by_name[name]))
        elif name == super_admin_name:
            # Synthesize the seeded admin's role if it isn't in
            # spec.roles — it's an implicit role that the seeded_admin
            # always has.
            rendered_roles.append(_render_role(RoleSpec(
                name=name,
                description="Platform super admin (seeded)",
                is_super_role=True,
            )))

    return {
        "id": _deterministic_uuid("application", app.name),
        "name": app.name,
        "roles": rendered_roles,
        "loginConfiguration": {
            "allowTokenRefresh": app.issues_refresh_tokens,
            "generateRefreshTokens": app.issues_refresh_tokens,
            "requireAuthentication": True,
        },
        "oauthConfiguration": {
            "authorizedRedirectURLs": app.redirect_urls,
            "logoutURL": app.logout_urls[0] if app.logout_urls else "",
            "requireClientAuthentication": app.pkce_required,
            "generateRefreshTokens": app.issues_refresh_tokens,
            "enabledGrants": ["authorization_code", "refresh_token"],
        },
    }


def _render_user_registration(
    spec: AuthSpec,
    user: UserSpec,
    app: AppSpec,
) -> Dict[str, Any]:
    """Roles for this user on this application.

    A user gets an application registration only if they have at least
    one role registered on that app (direct ``role_names`` or via a
    group whose application is this one).
    """
    direct_roles = set(user.role_names)

    # Roles via groups
    for group_name in user.group_names:
        for group in spec.groups:
            if group.name == group_name:
                if group.application is None or group.application == app.name:
                    direct_roles.update(group.role_names)

    app_role_set = set(app.role_names or spec.all_role_names())
    granted = sorted(direct_roles & app_role_set)
    if not granted:
        return {}

    return {
        "applicationId": _deterministic_uuid("application", app.name),
        "roles": granted,
    }


def _render_user(spec: AuthSpec, user: UserSpec) -> Dict[str, Any]:
    registrations = []
    for app in spec.applications:
        reg = _render_user_registration(spec, user, app)
        if reg:
            registrations.append(reg)

    return {
        "id": _deterministic_uuid("user", user.email),
        "email": user.email,
        "firstName": user.first_name,
        "lastName": user.last_name,
        "password": user.password,
        "passwordChangeRequired": user.password_change_required,
        "verified": user.verified,
        "registrations": registrations,
    }


def _render_seeded_admin(spec: AuthSpec) -> Dict[str, Any]:
    """Seeded admin gets a registration on every application so it can
    log in to all of them. Always present, never optional."""
    admin = spec.seeded_admin
    registrations = [
        {
            "applicationId": _deterministic_uuid("application", app.name),
            "roles": list(admin.role_names),
        }
        for app in spec.applications
    ]
    return {
        "id": _deterministic_uuid("user", admin.email),
        "email": admin.email,
        "firstName": admin.first_name,
        "lastName": admin.last_name,
        "password": admin.password,
        "passwordChangeRequired": admin.password_change_required,
        "verified": True,
        "registrations": registrations,
    }


def render_kickstart(spec: AuthSpec) -> Dict[str, Any]:
    """Pure function: ``AuthSpec`` → FusionAuth kickstart.json dict.

    Caller is responsible for serialization + writing to disk. Returns
    an empty kickstart shape when ``spec.enabled`` is False — provisioner
    should skip the FusionAuth container in that case, but this stays
    safe regardless.
    """
    if not spec.enabled:
        return {"variables": {}, "apiKeys": [], "requests": []}

    requests: List[Dict[str, Any]] = []

    # Applications (roles are registered as part of the application body)
    for app in spec.applications:
        body = _render_application(spec, app)
        requests.append({
            "method": "POST",
            "url": f"/api/application/{body['id']}",
            "body": {"application": body},
        })

    # Users (seeded admin first, then test users)
    requests.append({
        "method": "POST",
        "url": f"/api/user/{_deterministic_uuid('user', spec.seeded_admin.email)}",
        "body": {"user": _render_seeded_admin(spec)},
    })

    for user in spec.test_users:
        body = _render_user(spec, user)
        requests.append({
            "method": "POST",
            "url": f"/api/user/{body['id']}",
            "body": {"user": body},
        })

    # Groups (only if groups_enabled)
    if spec.groups_enabled:
        for group in spec.groups:
            group_id = _deterministic_uuid("group", group.name)
            app_id = (
                _deterministic_uuid("application", group.application)
                if group.application else None
            )
            role_grants = []
            if app_id:
                role_grants = [
                    {
                        "applicationId": app_id,
                        "roleId": _deterministic_uuid("role", rn),
                    }
                    for rn in group.role_names
                ]
            requests.append({
                "method": "POST",
                "url": f"/api/group/{group_id}",
                "body": {
                    "group": {
                        "id": group_id,
                        "name": group.name,
                        "description": group.description,
                        "roleIds": role_grants,
                    }
                },
            })

    return {
        "variables": {
            "defaultTenantId": _deterministic_uuid("tenant", "default"),
            "adminEmail": spec.seeded_admin.email,
            "adminPassword": spec.seeded_admin.password,
        },
        "apiKeys": [
            {
                "key": _deterministic_uuid("apikey", "bootstrap"),
                "description": "Bootstrap API key — bizniz orchestrator uses this",
            }
        ],
        "requests": requests,
    }
