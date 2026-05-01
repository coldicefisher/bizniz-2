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
    return _HEADER + body + _FOOTER
