"""Declarative auth state — the desired-state side of the auth contract.

This module defines what the planner emits (``AuthSpecDelta``), what the
architect rolls up (``AuthSpec``), and what the provisioner materializes
(via kickstart for fresh installs, via reconcile for live ones).

Why structured (and not freeform text from the planner): the orchestrator
is typed, every spec field is testable in isolation, deltas are git-
diffable across milestones, and the planner can't hallucinate a role
that doesn't fit the schema. See discussion notes
``docs/changes/2026-05-04_auth_spec.md``.

Counterpart to ``types.py``: that holds *observed* state (what FusionAuth
returns); this holds *desired* state (what we want it to be).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class RoleSpec(BaseModel):
    """A FusionAuth role, application-scoped."""
    name: str
    description: str = ""
    is_default: bool = False
    is_super_role: bool = False

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("role name cannot be empty")
        return v.strip()


class AppSpec(BaseModel):
    """A FusionAuth application — typically one per frontend that mints tokens.

    Backends share a JWKS via the issuer; only frontends need their own
    application/clientId/redirect_url config.
    """
    name: str
    redirect_urls: List[str] = Field(default_factory=list)
    logout_urls: List[str] = Field(default_factory=list)
    pkce_required: bool = True
    issues_refresh_tokens: bool = True
    # Roles registered against THIS application (subset of AuthSpec.roles
    # by name). Empty = inherits all roles from the spec.
    role_names: List[str] = Field(default_factory=list)


class GroupSpec(BaseModel):
    """A FusionAuth group — used for multi-tenancy when groups_enabled.

    A group bundles a set of role grants. Adding a user to a group grants
    them all the group's roles, scoped to the named application.
    """
    name: str
    description: str = ""
    application: Optional[str] = None  # AppSpec.name; None = global group
    role_names: List[str] = Field(default_factory=list)


class UserSpec(BaseModel):
    """A FusionAuth user — typically a test fixture seeded for integration tests."""
    email: str
    password: str = "password"  # dev/staging convention; production rotates
    first_name: str = ""
    last_name: str = ""
    role_names: List[str] = Field(default_factory=list)
    group_names: List[str] = Field(default_factory=list)
    password_change_required: bool = False
    verified: bool = True


class SeedAdminSpec(BaseModel):
    """The always-seeded super-admin. Constants — not a planner-emitted parameter.

    Every environment (dev, staging, production) ships with this user.
    Production sets ``password_change_required=True`` so the literal
    "password" cannot be used past the first login. The deployer is
    responsible for completing rotation during smoke test.
    """
    email: str = "admin@admin.com"
    password: str = "password"
    first_name: str = "Admin"
    last_name: str = "User"
    role_names: List[str] = Field(default_factory=lambda: ["super_admin"])
    password_change_required: bool = True


class DeprecatedRole(BaseModel):
    """Soft-deleted role marker. Production never hard-deletes roles from
    a milestone delta — data loss risk. Instead we mark them deprecated,
    log warnings on use, and surface them in the run report for explicit
    human decision.
    """
    name: str
    deprecated_at: str  # ISO 8601 UTC
    reason: str = ""


# ── Delta + cumulative spec ──────────────────────────────────────


class AuthSpecDelta(BaseModel):
    """What one milestone changes about auth. Empty delta = no change.

    Emitted by the planner alongside each ``Milestone``. The architect
    accumulates deltas in milestone order to produce the cumulative
    ``AuthSpec`` for that milestone's provisioner phase.

    Removals are soft-deletes: ``remove_roles`` moves names from
    ``AuthSpec.roles`` to ``AuthSpec.deprecated_roles``. The orchestrator
    will not delete them from FusionAuth without explicit human approval.
    """
    enable_auth: Optional[bool] = None
    enable_groups: Optional[bool] = None
    enable_multitenant: Optional[bool] = None
    add_roles: List[RoleSpec] = Field(default_factory=list)
    remove_roles: List[str] = Field(default_factory=list)  # names
    add_applications: List[AppSpec] = Field(default_factory=list)
    add_groups: List[GroupSpec] = Field(default_factory=list)
    add_test_users: List[UserSpec] = Field(default_factory=list)
    note: str = ""  # planner's free-text justification, advisory only

    def is_empty(self) -> bool:
        return not (
            self.enable_auth is not None
            or self.enable_groups is not None
            or self.enable_multitenant is not None
            or self.add_roles
            or self.remove_roles
            or self.add_applications
            or self.add_groups
            or self.add_test_users
        )


class AuthSpec(BaseModel):
    """Cumulative desired auth state at a point in time.

    Produced by ``baseline().apply(delta_M1).apply(delta_M2)...``. Used
    by both kickstart rendering (fresh installs) and reconcile (existing
    installs) — same input, same end state.

    The ``seeded_admin`` field is locked to its default in practice. Tests
    can override but production code paths should not — the determinism
    guarantee is "every generated app has admin@admin.com seeded."
    """
    enabled: bool = True
    multitenant: bool = False
    groups_enabled: bool = False
    roles: List[RoleSpec] = Field(default_factory=list)
    applications: List[AppSpec] = Field(default_factory=list)
    groups: List[GroupSpec] = Field(default_factory=list)
    test_users: List[UserSpec] = Field(default_factory=list)
    deprecated_roles: List[DeprecatedRole] = Field(default_factory=list)
    seeded_admin: SeedAdminSpec = Field(default_factory=SeedAdminSpec)

    @classmethod
    def baseline(cls) -> "AuthSpec":
        """Empty baseline. Auth disabled. M1 typically flips ``enable_auth``."""
        return cls(enabled=False)

    def apply(self, delta: AuthSpecDelta) -> "AuthSpec":
        """Return a new AuthSpec with the delta applied. Pure / immutable.

        Order of operations is fixed (toggles → roles → apps → groups →
        users → removals) so that two equivalent deltas produce the same
        end state regardless of construction order.
        """
        roles = list(self.roles)
        applications = list(self.applications)
        groups = list(self.groups)
        test_users = list(self.test_users)
        deprecated_roles = list(self.deprecated_roles)

        enabled = delta.enable_auth if delta.enable_auth is not None else self.enabled
        groups_enabled = (
            delta.enable_groups if delta.enable_groups is not None
            else self.groups_enabled
        )
        multitenant = (
            delta.enable_multitenant if delta.enable_multitenant is not None
            else self.multitenant
        )

        # Adds — dedup by name (later wins, milestone delta is authoritative).
        existing_role_names = {r.name for r in roles}
        for r in delta.add_roles:
            if r.name in existing_role_names:
                roles = [x for x in roles if x.name != r.name]
            roles.append(r)
            existing_role_names.add(r.name)

        existing_app_names = {a.name for a in applications}
        for a in delta.add_applications:
            if a.name in existing_app_names:
                applications = [x for x in applications if x.name != a.name]
            applications.append(a)
            existing_app_names.add(a.name)

        existing_group_names = {g.name for g in groups}
        for g in delta.add_groups:
            if g.name in existing_group_names:
                groups = [x for x in groups if x.name != g.name]
            groups.append(g)
            existing_group_names.add(g.name)

        existing_user_emails = {u.email for u in test_users}
        for u in delta.add_test_users:
            if u.email in existing_user_emails:
                test_users = [x for x in test_users if x.email != u.email]
            test_users.append(u)
            existing_user_emails.add(u.email)

        # Removals are soft — move to deprecated_roles, leave FusionAuth
        # alone until a human approves hard delete.
        if delta.remove_roles:
            now = datetime.now(timezone.utc).isoformat()
            for name in delta.remove_roles:
                if any(r.name == name for r in roles):
                    roles = [r for r in roles if r.name != name]
                    if not any(d.name == name for d in deprecated_roles):
                        deprecated_roles.append(
                            DeprecatedRole(name=name, deprecated_at=now)
                        )

        return AuthSpec(
            enabled=enabled,
            multitenant=multitenant,
            groups_enabled=groups_enabled,
            roles=roles,
            applications=applications,
            groups=groups,
            test_users=test_users,
            deprecated_roles=deprecated_roles,
            seeded_admin=self.seeded_admin,
        )

    def all_role_names(self) -> List[str]:
        """Active roles plus the seeded admin's roles. Used by kickstart
        to register every role on every application."""
        names = [r.name for r in self.roles]
        for r in self.seeded_admin.role_names:
            if r not in names:
                names.append(r)
        return names
