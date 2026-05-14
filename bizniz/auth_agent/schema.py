"""JSON schema for AuthAgent actions.

The AuthAgent's action shape is the union of:
  - FA-specific actions (fa_apply_spec, fa_smoke_login, fa_diagnose,
    fa_get_jwks, fa_get_tenant_issuer, fa_list_roles, fa_list_users)
  - Universal utility (decode_jwt)
  - Terminal action (submit_contract)

Strict-mode JSON schema. Every field required (filled with "" when
unused) so the LLM can't omit a field on actions that don't need it.
"""

AUTH_AGENT_ACTION_SCHEMA = {
    "name": "auth_agent_action",
    "strict": True,
    "schema": {
        "type": "object",
        "required": [
            "thinking",
            "action",
            "primary_app_id",
            "tenant_id",
            "email",
            "password",
            "spec_json",
            "token",
            "contract_markdown",
            "summary",
            "applied_changes",
        ],
        "properties": {
            "thinking": {
                "type": "string",
                "description": "Reasoning about the current state and what to do next.",
            },
            "action": {
                "type": "string",
                "enum": [
                    "fa_apply_spec",
                    "fa_smoke_login",
                    "fa_diagnose",
                    "fa_get_jwks",
                    "fa_get_tenant_issuer",
                    "fa_list_roles",
                    "fa_list_users",
                    "decode_jwt",
                    "submit_contract",
                ],
                "description": "The action to take.",
            },
            "primary_app_id": {
                "type": "string",
                "description": (
                    "FusionAuth application UUID this action targets. "
                    "Required for: fa_apply_spec, fa_smoke_login, "
                    "fa_diagnose, fa_list_roles, fa_list_users. "
                    "Empty string for actions that don't target an application."
                ),
            },
            "tenant_id": {
                "type": "string",
                "description": (
                    "FusionAuth tenant UUID. Required for fa_diagnose and "
                    "fa_get_tenant_issuer. Empty string otherwise."
                ),
            },
            "email": {
                "type": "string",
                "description": (
                    "Test user email. Used by fa_smoke_login (required), "
                    "and fa_diagnose (optional — when present, diagnose "
                    "also runs a live login round-trip)."
                ),
            },
            "password": {
                "type": "string",
                "description": (
                    "Test user password. Pairs with 'email' for "
                    "fa_smoke_login and fa_diagnose."
                ),
            },
            "spec_json": {
                "type": "string",
                "description": (
                    "JSON-encoded AuthSpec for fa_apply_spec. The agent "
                    "constructs this based on the milestone's auth needs "
                    "(roles, applications, test users). Empty for non-apply "
                    "actions."
                ),
            },
            "token": {
                "type": "string",
                "description": (
                    "JWT string for decode_jwt. Typically obtained from a "
                    "preceding fa_smoke_login. Empty for other actions."
                ),
            },
            "contract_markdown": {
                "type": "string",
                "description": (
                    "Full AUTH_CONTRACT.md markdown body. ONLY used with "
                    "submit_contract. Must include: provider info, token "
                    "format (alg, iss, aud, claims), JWKS URL, test users, "
                    "and code samples for verifying tokens in EACH language "
                    "the project's stack uses (Python python-jose, "
                    "TypeScript jose, etc.)."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "ONLY used with submit_contract. One-paragraph "
                    "narrative of what the agent did or found "
                    "(configure mode: what was applied; audit mode: "
                    "what was verified, what drifted)."
                ),
            },
            "applied_changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "ONLY used with submit_contract. List of state changes "
                    "the agent applied (configure mode). Empty list in "
                    "audit mode."
                ),
            },
        },
        "additionalProperties": False,
    },
}
