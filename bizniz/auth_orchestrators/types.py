"""Typed entities for FusionAuth orchestration."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# Type aliases (string-typed identifiers — FusionAuth uses UUIDs as strings).
ApplicationId = str
RoleId = str
UserId = str


@dataclass(frozen=True)
class FusionAuthRole:
    role_id: RoleId
    name: str
    description: Optional[str] = None
    is_default: bool = False
    is_super_role: bool = False


@dataclass(frozen=True)
class FusionAuthUser:
    user_id: UserId
    email: str
    roles: List[str] = field(default_factory=list)  # role names, not IDs
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    active: bool = True
    verified: bool = False


@dataclass
class FusionAuthState:
    """Snapshot of FusionAuth's current state for an application.

    Returned by ``orchestrator.crawl(app_id)``. Used as input to
    ``reconcile()`` to compute a diff against the desired state.
    """
    application_id: ApplicationId
    application_name: str
    roles: List[FusionAuthRole] = field(default_factory=list)
    users: List[FusionAuthUser] = field(default_factory=list)


@dataclass
class ReconcileAction:
    """One operation that ``reconcile()`` will (or did) perform.

    Created with ``applied=False`` during dry-run / preview, set to
    ``True`` after execution.
    """
    operation: str          # "create_role", "delete_user", "assign_role", ...
    target: str             # "role:landlord", "user:tenant@example.com", ...
    detail: str = ""        # human-readable description
    applied: bool = False
    error: Optional[str] = None


@dataclass
class ReconcileReport:
    """Outcome of a reconcile() call: planned actions + their results."""
    actions: List[ReconcileAction] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return all(a.applied and a.error is None for a in self.actions)

    @property
    def applied_count(self) -> int:
        return sum(1 for a in self.actions if a.applied and a.error is None)

    @property
    def failed_count(self) -> int:
        return sum(1 for a in self.actions if a.error is not None)


class FusionAuthError(RuntimeError):
    """Raised when FusionAuth returns an unexpected status or the API
    is unreachable. Idempotent operations (ensure_X) only raise on
    actual failures, not on "already exists" responses."""
    def __init__(self, message: str, status_code: Optional[int] = None,
                 response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
