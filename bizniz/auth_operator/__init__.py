"""FusionAuthOperator — deterministic FA materialization.

Takes an AuthSpec from the AuthPlanner and makes FusionAuth match it.
Owns every FA quirk in one place:
  - readiness wait (api_key actually authenticates)
  - RS256 signing key generation + binding
  - role + application creation
  - user creation with role registration
  - smoke-login verification
  - retry on transient connection errors

Returns an ``AuthManifest`` describing what FusionAuth actually has
post-apply. The manifest is the source of truth for everything
downstream: contract markdown rendering, contract test rendering,
audit verification.

No LLM. No prompts. All deterministic.
"""
from bizniz.auth_operator.code_examples import generate_code_examples
from bizniz.auth_operator.contract_renderer import render_auth_contract
from bizniz.auth_operator.manifest import (
    AuthManifest, RoleManifest, SigningKeyInfo, UserManifest,
)
from bizniz.auth_operator.operator import (
    FusionAuthOperator, FusionAuthOperatorError,
)

__all__ = [
    "AuthManifest", "FusionAuthOperator", "FusionAuthOperatorError",
    "RoleManifest", "SigningKeyInfo", "UserManifest",
    "generate_code_examples",
    "render_auth_contract",
]
