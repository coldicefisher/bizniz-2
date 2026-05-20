"""``workspace_context`` — preventive context layer.

Every agent call's prompt gets a snapshot of "what's actually true
right now" so the agent stops guessing about its operating
environment. The 2026-05-19/20 v9/v10 debriefs identified this as
the root cause behind half a dozen apparently-distinct failure
modes (agent imported wrong library, ran tests against missing
container, invented symbols that don't exist, etc.).

WorkspaceContext combines:
  - Live file content for the issue's target/test files
  - Declared dependencies (parsed from requirements.txt etc.)
  - Import-name mapping (jwt → pyjwt, jose → python-jose, ...)
  - (later) Installed-package truth from `pip list` in container
  - (later) Symbol index across the workspace
  - (later) Sibling-issue state for parallel awareness

Rendered to a prompt section the agent reads BEFORE writing code.
"""
from bizniz.workspace_context.builder import (
    WorkspaceContextBuilder,
)
from bizniz.workspace_context.types import (
    DeclaredPackage,
    WorkspaceContext,
)

__all__ = [
    "DeclaredPackage",
    "WorkspaceContext",
    "WorkspaceContextBuilder",
]
