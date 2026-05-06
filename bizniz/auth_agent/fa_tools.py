"""High-level FA tool wrappers exposed to the AuthAgent's tool loop.

Wraps ``FusionAuthOrchestrator`` typed methods as discrete tools the
agent can call from its action schema. Deliberately few + high-level —
the orchestrator has 30+ methods, but the agent only needs a handful
of focused operations:

  - ``fa_apply_spec``        — materialize an AuthSpec (mutating)
  - ``fa_smoke_login``       — log in as a test user, return JWT
  - ``fa_diagnose``          — run the orchestrator's diagnostic battery
  - ``fa_get_jwks``          — fetch the JWKS doc
  - ``fa_get_tenant_issuer`` — read the tenant's actual ``issuer`` setting
  - ``fa_list_roles``        — enumerate roles configured for an application
  - ``fa_list_users``        — enumerate users registered with an application

In ``audit`` mode the AuthAgent's tool_handlers exclude the mutating
tools (``fa_apply_spec``). The factory ``build_fa_handlers(...,
audit_mode=True)`` returns a read-only subset.

Each handler returns a string fed back into the conversation.
Handlers format errors as ``"ERROR: <reason>"`` rather than raising —
the loop is expected to recover and try a different action.
"""
from __future__ import annotations

import base64
import json
from typing import Callable, Dict

from bizniz.auth.fusionauth_orchestrator import FusionAuthOrchestrator
from bizniz.auth.spec import AuthSpec
from bizniz.auth.types import FusionAuthError


ToolHandler = Callable[[Dict], str]


def _truncate(s: str, n: int = 4000) -> str:
    return s if len(s) <= n else s[:n] + "\n\n... (truncated)"


def make_fa_apply_spec(orch: FusionAuthOrchestrator) -> ToolHandler:
    """Materialize an AuthSpec against live FA. Mutating — only included
    in ``configure`` mode tool surface."""
    def handler(action: Dict) -> str:
        spec_json = action.get("spec_json", "")
        primary_app_id = action.get("primary_app_id", "")
        if not spec_json or not primary_app_id:
            return (
                "ERROR: fa_apply_spec requires both 'spec_json' (the AuthSpec "
                "as a JSON string) and 'primary_app_id' (the app UUID this "
                "spec targets)."
            )
        try:
            spec_dict = json.loads(spec_json)
            spec = AuthSpec.model_validate(spec_dict)
        except Exception as e:
            return f"ERROR: spec_json could not be parsed as AuthSpec: {e}"
        try:
            report = orch.materialize(spec, primary_app_id=primary_app_id)
        except FusionAuthError as e:
            return f"ERROR: materialize raised FusionAuthError: {e}"
        except Exception as e:
            return f"ERROR: materialize raised {type(e).__name__}: {e}"

        applied = sum(1 for a in report.actions if a.applied)
        failed = sum(1 for a in report.actions if a.error)
        lines = [
            f"materialize complete: {applied} applied, {failed} failed",
        ]
        for a in report.actions:
            tag = "APPLIED" if a.applied else ("FAILED" if a.error else "skip")
            line = f"  [{tag}] {a.operation} {a.target}"
            if a.error:
                line += f" — {a.error}"
            lines.append(line)
        return _truncate("\n".join(lines))
    return handler


def make_fa_smoke_login(orch: FusionAuthOrchestrator) -> ToolHandler:
    """Log in a test user via FA's /api/login. Returns the JWT (so the
    agent can pass it to decode_jwt). Read-only against FA state."""
    def handler(action: Dict) -> str:
        app_id = action.get("primary_app_id", "")
        email = action.get("email", "")
        password = action.get("password", "")
        if not (app_id and email and password):
            return (
                "ERROR: fa_smoke_login requires 'primary_app_id', 'email', "
                "'password'."
            )
        try:
            token = orch.get_token(app_id, email, password)
        except FusionAuthError as e:
            return f"login failed: {e}"
        except Exception as e:
            return f"ERROR: login raised {type(e).__name__}: {e}"
        return (
            f"login OK for {email}\n"
            f"jwt (length={len(token)}):\n{token}\n\n"
            f"Pass this token to decode_jwt to inspect its claims."
        )
    return handler


def make_fa_diagnose(orch: FusionAuthOrchestrator) -> ToolHandler:
    """Run the orchestrator's diagnostic battery: tenant issuer, JWKS
    keys, signing-key alg, optional live login round-trip."""
    def handler(action: Dict) -> str:
        app_id = action.get("primary_app_id", "")
        tenant_id = action.get("tenant_id", "")
        test_email = action.get("email") or None
        test_password = action.get("password") or None
        if not (app_id and tenant_id):
            return "ERROR: fa_diagnose requires 'primary_app_id' and 'tenant_id'."
        try:
            report = orch.diagnose(
                app_id=app_id,
                tenant_id=tenant_id,
                test_email=test_email,
                test_password=test_password,
            )
        except Exception as e:
            return f"ERROR: diagnose raised {type(e).__name__}: {e}"
        return _truncate(json.dumps(report, indent=2))
    return handler


def make_fa_get_jwks(orch: FusionAuthOrchestrator) -> ToolHandler:
    def handler(action: Dict) -> str:
        try:
            jwks = orch.get_jwks()
        except Exception as e:
            return f"ERROR: get_jwks raised {type(e).__name__}: {e}"
        return _truncate(json.dumps(jwks, indent=2))
    return handler


def make_fa_get_tenant_issuer(orch: FusionAuthOrchestrator) -> ToolHandler:
    """Returns the tenant's configured ``issuer`` field. Note: the
    actual ``iss`` claim FA puts on JWTs may differ if the tenant's
    issuer setting is unset (FA defaults to ``acme.com`` on a fresh
    tenant). To get the LIVE iss claim, do a smoke_login + decode_jwt."""
    def handler(action: Dict) -> str:
        tenant_id = action.get("tenant_id", "")
        if not tenant_id:
            return "ERROR: fa_get_tenant_issuer requires 'tenant_id'."
        try:
            tenant = orch.get_tenant(tenant_id)
        except Exception as e:
            return f"ERROR: get_tenant raised {type(e).__name__}: {e}"
        if not tenant:
            return f"tenant {tenant_id} not found"
        issuer = (tenant.get("tenant") or {}).get("issuer", "(unset)")
        return f"tenant.issuer = {issuer!r}"
    return handler


def make_fa_list_roles(orch: FusionAuthOrchestrator) -> ToolHandler:
    def handler(action: Dict) -> str:
        app_id = action.get("primary_app_id", "")
        if not app_id:
            return "ERROR: fa_list_roles requires 'primary_app_id'."
        try:
            roles = orch.list_roles(app_id)
        except Exception as e:
            return f"ERROR: list_roles raised {type(e).__name__}: {e}"
        if not roles:
            return f"(no roles configured on application {app_id})"
        lines = [f"{len(roles)} role(s) on application {app_id}:"]
        for r in roles:
            default = " [default]" if r.is_default else ""
            super_role = " [superRole]" if r.is_super_role else ""
            lines.append(f"  - {r.name}{default}{super_role}: {r.description}")
        return "\n".join(lines)
    return handler


def make_fa_list_users(orch: FusionAuthOrchestrator) -> ToolHandler:
    def handler(action: Dict) -> str:
        app_id = action.get("primary_app_id", "")
        if not app_id:
            return "ERROR: fa_list_users requires 'primary_app_id'."
        try:
            res = orch.request(
                "POST", "/api/user/search",
                body={"search": {
                    "query": (
                        '{"bool":{"must":[{"nested":{"path":"registrations",'
                        '"query":{"term":{"registrations.applicationId":"'
                        + app_id + '"}}}}]}}'
                    ),
                    "numberOfResults": 100,
                }},
            )
        except Exception as e:
            return f"ERROR: user search raised {type(e).__name__}: {e}"
        users = res.get("users") or []
        if not users:
            return f"(no users registered with application {app_id})"
        lines = [f"{len(users)} user(s) on application {app_id}:"]
        for u in users[:50]:
            email = u.get("email", "?")
            uid = u.get("id", "?")
            verified = u.get("verified", False)
            lines.append(
                f"  - {email} (id={uid}) "
                f"verified={verified}"
            )
        if len(users) > 50:
            lines.append(f"  ... ({len(users) - 50} more)")
        return "\n".join(lines)
    return handler


def make_decode_jwt() -> ToolHandler:
    """Decode a JWT's header + payload without verifying. Pure utility."""
    def handler(action: Dict) -> str:
        token = (action.get("token") or "").strip()
        if not token:
            return "ERROR: decode_jwt requires a non-empty 'token'."
        if token.lower().startswith("bearer "):
            token = token.split(None, 1)[1]
        parts = token.split(".")
        if len(parts) != 3:
            return f"ERROR: not a JWT (expected 3 parts, got {len(parts)})."
        try:
            def _decode(seg: str) -> dict:
                pad = seg + "=" * (-len(seg) % 4)
                return json.loads(base64.urlsafe_b64decode(pad.encode()))
            header = _decode(parts[0])
            payload = _decode(parts[1])
        except Exception as e:
            return f"ERROR: could not decode: {e}"
        return (
            f"=== JWT (signature NOT verified) ===\n"
            f"Header:\n{json.dumps(header, indent=2)}\n\n"
            f"Payload:\n{json.dumps(payload, indent=2)}"
        )
    return handler


def build_fa_handlers(
    orch: FusionAuthOrchestrator,
    audit_mode: bool = False,
) -> Dict[str, ToolHandler]:
    """Build the FA-specific tool handler dict.

    In ``audit_mode=True``, mutating tools (currently just
    ``fa_apply_spec``) are excluded so the agent cannot apply changes.
    """
    handlers: Dict[str, ToolHandler] = {
        "fa_smoke_login": make_fa_smoke_login(orch),
        "fa_diagnose": make_fa_diagnose(orch),
        "fa_get_jwks": make_fa_get_jwks(orch),
        "fa_get_tenant_issuer": make_fa_get_tenant_issuer(orch),
        "fa_list_roles": make_fa_list_roles(orch),
        "fa_list_users": make_fa_list_users(orch),
        "decode_jwt": make_decode_jwt(),
    }
    if not audit_mode:
        handlers["fa_apply_spec"] = make_fa_apply_spec(orch)
    return handlers
