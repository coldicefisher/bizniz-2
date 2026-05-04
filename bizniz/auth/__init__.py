"""Auth orchestration.

Centralizes FusionAuth-related operations behind a typed,
deterministic API. Pipeline components (architect, engineer,
integration testers, FusionAuth provisioner) all route through
``FusionAuthOrchestrator`` instead of hand-rolling their own
FusionAuth API calls. Single source of truth for FusionAuth state
within a project.

Most operations are idempotent (``ensure_application``,
``ensure_role``, ``ensure_user``) so they're safe to call from
provisioning AND from runtime app code that needs to add roles
at runtime. A generic ``request()`` escape hatch lets callers
hit FusionAuth endpoints we haven't typed yet.
"""
from bizniz.auth.types import (
    ApplicationId,
    RoleId,
    UserId,
    FusionAuthRole,
    FusionAuthUser,
    FusionAuthState,
    ReconcileAction,
    ReconcileReport,
    FusionAuthError,
)
from bizniz.auth.fusionauth_orchestrator import FusionAuthOrchestrator
from bizniz.auth.contract import (
    AuthContract,
    ContractRole,
    ContractTestUser,
    ContractEndpoint,
    JwtClaimContract,
    RuntimeContract,
    ValidationCheck,
    ContractValidationResult,
)

__all__ = [
    "ApplicationId",
    "RoleId",
    "UserId",
    "FusionAuthRole",
    "FusionAuthUser",
    "FusionAuthState",
    "ReconcileAction",
    "ReconcileReport",
    "FusionAuthError",
    "FusionAuthOrchestrator",
    "AuthContract",
    "ContractRole",
    "ContractTestUser",
    "ContractEndpoint",
    "JwtClaimContract",
    "RuntimeContract",
    "ValidationCheck",
    "ContractValidationResult",
]
