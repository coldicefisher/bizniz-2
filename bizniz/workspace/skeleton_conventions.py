"""Read a workspace's SKELETON.md and turn it into a system-prompt section.

Skeletons that ship a SKELETON.md at workspace root document directory
contracts the AI must respect — e.g. "FastAPI routers go in
app/api/routes/, do not edit app/main.py." Without this, agents
generate parallel package trees that aren't reachable from the running
container's entrypoint.

The orchestrator/engineer call ``load_skeleton_conventions`` once at
construction and append the result to coder, tester, and engineer
system prompts.
"""
from __future__ import annotations

from typing import Optional

from bizniz.workspace.base_workspace import BaseWorkspace


_HEADER = "## Skeleton directory contract (HARD CONSTRAINT)\n\n"
_FOOTER = (
    "\n\n**Violation symptom:** code outside the skeleton's extension "
    "points is not reachable from the running container's entrypoint. "
    "Tests may pass (they import modules directly) but the deployed "
    "stack will be missing the endpoints/components you wrote."
)


def load_skeleton_conventions(workspace: BaseWorkspace) -> Optional[str]:
    """Return SKELETON.md wrapped as a system-prompt section, or None."""
    try:
        skel_path = workspace.path("SKELETON.md")
    except Exception:
        return None
    if not skel_path.exists() or not skel_path.is_file():
        return None
    try:
        body = skel_path.read_text()
    except Exception:
        return None
    body = body.strip()
    if not body:
        return None

    # Also load AUTH_CONTRACT.md from project root (parent of workspace)
    # if it exists. This gives the engineer the FusionAuth configuration
    # (roles, test users, endpoints) alongside the skeleton contract.
    auth_section = ""
    try:
        # Walk up from workspace to find AUTH_CONTRACT.md at project root
        project_root = workspace.root.parent
        auth_path = project_root / "AUTH_CONTRACT.md"
        if auth_path.exists() and auth_path.is_file():
            auth_body = auth_path.read_text().strip()
            if auth_body:
                auth_section = (
                    "\n\n## Auth Contract (HARD CONSTRAINT)\n\n"
                    + auth_body
                    + "\n\n**Do NOT implement your own auth.** Use get_current_user "
                    "and require_roles from app.core.auth. FusionAuth handles "
                    "registration, login, tokens, email verification, and "
                    "password reset."
                )
    except Exception:
        pass

    return _HEADER + body + _FOOTER + auth_section
