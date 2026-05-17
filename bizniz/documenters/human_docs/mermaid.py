"""Mermaid diagram renderers for human docs.

Pure deterministic — given an ``architecture.SystemArchitecture``,
emit Mermaid syntax that renders as a directed graph of services
+ their dependencies. Embedded inline in ``architecture.md``.

GitHub renders Mermaid natively, so the docs work without a build
step. Markdown viewers that don't render Mermaid still show the
source as a code block — useful but not broken.
"""
from __future__ import annotations

from typing import Iterable

from bizniz.architect.types import ServiceDefinition, SystemArchitecture


# Shape choices map service_type to a Mermaid node shape so the
# diagram conveys role at a glance.
_SHAPE_BY_TYPE = {
    "backend": ("[", "]"),         # rectangle
    "frontend": ("(", ")"),        # rounded rectangle
    "worker": ("[/", "/]"),        # parallelogram (data flow)
    "database": ("[(", ")]"),      # cylinder
    "cache": ("[(", ")]"),         # cylinder
    "auth": ("{{", "}}"),          # hexagon
    "queue": ("[/", "/]"),         # parallelogram
}


def _node_def(svc: ServiceDefinition) -> str:
    open_, close = _SHAPE_BY_TYPE.get(svc.service_type or "", ("[", "]"))
    framework = svc.framework or "?"
    return (
        f"    {_safe_id(svc.name)}{open_}"
        f'"{svc.name}<br/><small>{framework}</small>"{close}'
    )


def _safe_id(name: str) -> str:
    """Mermaid node IDs can't contain hyphens/spaces — sanitize."""
    return "".join(c if c.isalnum() else "_" for c in name)


def render_service_graph(arch: SystemArchitecture) -> str:
    """Render the project's service dependency graph as Mermaid.

    Returns the full ```mermaid``` fenced block ready to paste into
    a Markdown doc. Services with no dependencies appear as isolated
    nodes (still rendered)."""
    lines = ["```mermaid", "graph TD"]
    for svc in arch.services:
        lines.append(_node_def(svc))
    # Edges — from dependent to dependency.
    for svc in arch.services:
        for dep in svc.depends_on or []:
            # Validate the dep exists in the architecture.
            if any(s.name == dep for s in arch.services):
                lines.append(
                    f"    {_safe_id(svc.name)} --> {_safe_id(dep)}"
                )
    lines.append("```")
    return "\n".join(lines)
