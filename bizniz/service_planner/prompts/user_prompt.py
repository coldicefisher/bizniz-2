"""Build the user-facing prompt for one ServicePlanner call."""
from __future__ import annotations

from typing import Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.lib.framework_conventions import render_for_engineer
from bizniz.quality_engineer.types import EnrichedSpec


def build_service_planner_prompt(
    *,
    architecture: SystemArchitecture,
    enriched_spec: EnrichedSpec,
    service: ServiceDefinition,
    skeleton_md: Optional[str] = None,
    auth_contract: Optional[str] = None,
) -> str:
    """Assemble the user prompt for ServicePlanner.plan_service().

    Includes:
      - The full EnrichedSpec (capabilities the issues should deliver)
      - The full architecture summary (so cross-service deps are visible)
      - This service's definition (target framework, language, depends_on)
      - The framework conventions for this service's framework
      - Skeleton contract + auth contract when present
    """
    sections: list[str] = []

    sections.append(f"## Target service\n\n{_render_service(service)}")

    sections.append("## Capabilities to deliver (from EnrichedSpec)\n")
    sections.append(_render_capabilities(enriched_spec))

    sections.append("## System architecture (for cross-service context)\n")
    sections.append(_render_architecture(architecture))

    fw = render_for_engineer(architecture)
    if fw:
        sections.append(fw)

    if skeleton_md:
        sections.append(
            "## Skeleton directory contract\n\n"
            "The Coder MUST place new files inside the skeleton's "
            "extension points only. Files outside are dead code.\n\n"
            f"{skeleton_md}"
        )

    if auth_contract:
        sections.append(
            "## Auth contract (FusionAuth-issued)\n\n"
            f"{auth_contract}"
        )

    sections.append(
        "## Your job\n\n"
        f"Decompose the **{service.name}** service's slice of this "
        "milestone into discrete coding issues that the Coder can "
        "implement one at a time. Output a JSON object with an "
        "`issues` array. Order issues so domain models / shared types "
        "come first; routes, services, and integration code come "
        "after their domain dependencies. Use the depends_on field "
        "to encode the topological order — the Orchestrator will "
        "respect it."
    )

    return "\n\n".join(sections)


def _render_service(service: ServiceDefinition) -> str:
    deps = ", ".join(service.depends_on) if service.depends_on else "—"
    lines = [
        f"- name: `{service.name}`",
        f"- type: {service.service_type}",
        f"- framework: {service.framework}",
        f"- language: {service.language}",
        f"- workspace: `{service.workspace_name}/`",
        f"- port: {service.port}",
        f"- depends_on: {deps}",
        f"- description: {service.description}",
    ]
    if service.skeleton:
        lines.append(f"- skeleton: {service.skeleton}")
    return "\n".join(lines)


def _render_capabilities(spec: EnrichedSpec) -> str:
    if not spec.capabilities:
        return "(no capabilities in spec — this is suspicious; spec was empty)"
    lines: list[str] = []
    for c in spec.capabilities:
        lines.append(f"### `{c.id}` — {c.name}")
        if c.description:
            lines.append(c.description)
        if c.inputs:
            lines.append("**Inputs:**")
            for f in c.inputs:
                req = "REQUIRED" if f.required else "optional"
                cons = f" ({'; '.join(f.constraints)})" if f.constraints else ""
                lines.append(f"  - `{f.name}: {f.type}` — {req}{cons}")
        if c.outputs:
            lines.append("**Outputs:**")
            for f in c.outputs:
                lines.append(f"  - `{f.name}: {f.type}`")
        if c.validation_rules:
            lines.append("**Validation rules:**")
            for r in c.validation_rules:
                lines.append(f"  - {r}")
        if c.error_cases:
            lines.append("**Error cases:**")
            for e in c.error_cases:
                lines.append(f"  - {e}")
        if c.allowed_roles:
            lines.append(f"**Allowed roles:** {', '.join(c.allowed_roles)}")
        lines.append("")
    return "\n".join(lines)


def _render_architecture(arch: SystemArchitecture) -> str:
    lines = [f"Project: **{arch.project_name}** (`{arch.project_slug}`)"]
    if arch.description:
        lines.append(arch.description)
    lines.append("\nServices:")
    for s in arch.services:
        deps = ", ".join(s.depends_on) if s.depends_on else "—"
        lines.append(
            f"  - `{s.name}` ({s.service_type}/{s.framework}, "
            f"{s.language}, port {s.port}, depends_on: {deps})"
        )
    return "\n".join(lines)
