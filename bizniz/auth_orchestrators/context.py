"""Loaders for auth context that downstream agents inject into prompts.

There are two artifacts at the project root that together describe auth:

- ``AUTH_CONTRACT.md`` — verified-reality (provisioner-emitted, validated
  against live FusionAuth). Tells the agent what test users exist, what
  endpoints accept tokens, what claims live in the JWT.
- ``docs/auth/spec.json`` — intent (planner-emitted, accumulated across
  milestones). Tells the agent what roles/applications/groups the
  pipeline thinks should exist. Useful when the agent is reasoning
  about *what to build*, not just what's been verified.

The engineer and coder both inject the combined context into their
prompts so they reason from the same source of truth as the integration
testers and debugger.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def _project_root_from_workspace(workspace) -> Optional[Path]:
    """Service workspaces are nested under the project root. Walk up
    until we find a directory containing AUTH_CONTRACT.md or
    docs/auth/spec.json. Return None if neither found.

    We don't use a fixed parent depth because workspace layout varies
    (backend/, services/auth/, frontend/web/, ...).
    """
    try:
        root = Path(str(workspace.path("")))
    except Exception:
        return None
    for candidate in (root, *root.parents):
        if (candidate / "AUTH_CONTRACT.md").is_file():
            return candidate
        if (candidate / "docs" / "auth" / "spec.json").is_file():
            return candidate
    return None


def load_auth_context_for_prompt(workspace) -> str:
    """Format AUTH_CONTRACT.md + spec.json as a prompt section.

    Returns an empty string if neither file is present (auth-disabled
    project, or this is a non-auth-touching milestone). Caller should
    inject the result into a ``{auth_context}`` template slot.
    """
    project_root = _project_root_from_workspace(workspace)
    if project_root is None:
        return ""

    sections = []

    contract_path = project_root / "AUTH_CONTRACT.md"
    if contract_path.is_file():
        try:
            contract_text = contract_path.read_text()
            sections.append(
                "## Auth Contract (verified against live FusionAuth)\n\n"
                "This contract describes the authentication state THAT EXISTS RIGHT NOW.\n"
                "Test users below are real and can be logged in with the listed passwords.\n"
                "JWT claims, issuer, audience, JWKS URL — all verified.\n\n"
                f"{contract_text}"
            )
        except Exception:
            pass

    spec_path = project_root / "docs" / "auth" / "spec.json"
    if spec_path.is_file():
        try:
            spec_data = json.loads(spec_path.read_text())
            sections.append(_format_spec_section(spec_data))
        except Exception:
            pass

    if not sections:
        return ""

    header = (
        "# Authentication context\n\n"
        "Auth in this project is delegated to FusionAuth. The skeleton "
        "validates FusionAuth-issued JWTs via JWKS — DO NOT mint tokens, "
        "hash passwords, or store sessions yourself. Use the contract "
        "below as the source of truth for endpoint shapes, role names, "
        "and test users.\n"
    )
    return header + "\n\n".join(sections)


def _format_spec_section(spec_data: dict) -> str:
    """Render the AuthSpec JSON sidecar as a compact prompt section.

    Showing intent (planner) alongside verified-reality (contract) lets
    the engineer reason about roles/apps/groups even before they exist
    in FusionAuth — useful when planning code that introduces the auth
    state for the first time.
    """
    roles = spec_data.get("roles") or []
    apps = spec_data.get("applications") or []
    groups = spec_data.get("groups") or []
    test_users = spec_data.get("test_users") or []
    deprecated = spec_data.get("deprecated_roles") or []
    seeded = spec_data.get("seeded_admin") or {}

    lines = [
        "## Auth Spec (cumulative intent across milestones)",
        "",
        f"- Auth enabled: {spec_data.get('enabled', False)}",
        f"- Multi-tenant: {spec_data.get('multitenant', False)}",
        f"- Groups enabled: {spec_data.get('groups_enabled', False)}",
        "",
    ]

    if roles:
        lines.append("### Roles")
        for r in roles:
            d = " (default)" if r.get("is_default") else ""
            s = " [super]" if r.get("is_super_role") else ""
            lines.append(f"- **{r.get('name')}**{d}{s}: {r.get('description', '')}")
        lines.append("")

    if apps:
        lines.append("### Applications (one per token-minting frontend)")
        for a in apps:
            redirects = ", ".join(a.get("redirect_urls") or []) or "(none)"
            lines.append(
                f"- **{a.get('name')}** — pkce={a.get('pkce_required', True)}, "
                f"refresh={a.get('issues_refresh_tokens', True)}, "
                f"redirects: {redirects}"
            )
        lines.append("")

    if groups:
        lines.append("### Groups (multi-tenancy)")
        for g in groups:
            lines.append(
                f"- **{g.get('name')}** ({g.get('application') or 'global'}): "
                f"roles={g.get('role_names', [])}"
            )
        lines.append("")

    if test_users:
        lines.append("### Test users (seeded by provisioner)")
        for u in test_users:
            lines.append(
                f"- {u.get('email')} (password: {u.get('password', 'password')}) "
                f"— roles: {u.get('role_names', [])}, "
                f"groups: {u.get('group_names', [])}"
            )
        lines.append("")

    if seeded:
        lines.append(
            f"### Seeded super-admin (always present)\n"
            f"- {seeded.get('email')} (password: {seeded.get('password')}) "
            f"— roles: {seeded.get('role_names')}\n"
        )

    if deprecated:
        lines.append("### Deprecated roles (soft-deleted, still in FusionAuth)")
        for d in deprecated:
            lines.append(
                f"- ~~{d.get('name')}~~ — deprecated {d.get('deprecated_at')}"
            )
        lines.append("")

    return "\n".join(lines)
