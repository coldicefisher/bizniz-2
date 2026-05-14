"""AuthManifest — what FusionAuth actually has post-FusionAuthOperator.apply().

This is the source of truth for downstream consumers (contract
markdown, contract tests, audit). Built from live FA state, not
from the spec — so it reflects reality, not intent.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class SigningKeyInfo(BaseModel):
    """The active JWT signing key bound to the primary application
    and/or tenant."""
    key_id: str
    algorithm: str  # "RS256", "RS384", "RS512", "HS256" (last is bad)
    bound_to_app: bool = False
    bound_to_tenant: bool = False

    @property
    def is_rs_family(self) -> bool:
        return self.algorithm.upper().startswith("RS")


class RoleManifest(BaseModel):
    """A role registered on the primary application."""
    name: str
    description: str = ""
    is_super_role: bool = False


class UserManifest(BaseModel):
    """A user that exists in FA and (post-apply) is registered on
    the primary app with the listed roles."""
    email: str
    user_id: str
    password: str  # for test users; production rotates
    first_name: str = ""
    last_name: str = ""
    roles: List[str] = Field(default_factory=list)
    registered: bool = False  # registered on primary app
    login_verified: bool = False  # /api/login returned a token


class ApplicationManifest(BaseModel):
    name: str
    application_id: str
    role_names: List[str] = Field(default_factory=list)


class AuthManifest(BaseModel):
    """Snapshot of the FA configuration FusionAuthOperator established."""
    fa_url: str
    primary_app_id: str
    tenant_id: str
    issuer: str = ""
    signing_key: SigningKeyInfo
    applications: List[ApplicationManifest] = Field(default_factory=list)
    roles: List[RoleManifest] = Field(default_factory=list)
    users: List[UserManifest] = Field(default_factory=list)

    @property
    def all_users_login_verified(self) -> bool:
        return all(u.login_verified for u in self.users) if self.users else True

    def role_names(self) -> List[str]:
        return [r.name for r in self.roles]
