"""Initial user message for Coder.code_issue.

Narrow scope: ONE issue + the capabilities its spec_refs point to +
the relevant skeleton conventions for the issue's service. NOT the
whole EnrichedSpec, NOT all services, NOT all issues.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.coder.types import Issue
from bizniz.lib.framework_conventions import render_for_engineer
from bizniz.quality_engineer.types import CapabilitySpec, EnrichedSpec


def build_coder_initial_context(
    *,
    issue: Issue,
    architecture: SystemArchitecture,
    enriched_spec: EnrichedSpec,
    auth_contract: Optional[str] = None,
    workspace_summary: Optional[str] = None,
    skeleton_md: Optional[str] = None,
) -> str:
    """Compose the initial user message.

    Filters EnrichedSpec.capabilities to ONLY the ones referenced by
    ``issue.spec_refs`` so the Coder doesn't drown in unrelated spec
    details. Filters architecture services to ONLY the issue's
    service + its declared depends_on (so the Coder knows about
    direct dependencies without seeing the whole stack).
    """
    parts: List[str] = [f"# Issue: {issue.id} — {issue.title}\n"]

    parts.append(f"\n## Description\n{issue.description.strip()}\n")

    if issue.target_files:
        parts.append("\n## Target files (write these)\n")
        for f in issue.target_files:
            parts.append(f"- `{f}`\n")
    if issue.test_files:
        parts.append("\n## Test files (write these AFTER source passes validate_symbols)\n")
        for f in issue.test_files:
            parts.append(f"- `{f}`\n")
    if issue.success_criteria:
        parts.append("\n## Success criteria\n")
        for c in issue.success_criteria:
            parts.append(f"- {c}\n")

    parts.append(
        f"\n## Service\n"
        f"This issue lives in service `{issue.service}` "
        f"(language: {issue.language}). Tools that take ``service`` "
        f"default to this service.\n"
    )

    # Direct service deps only — not the whole architecture.
    own_service = next(
        (s for s in architecture.services if s.name == issue.service), None,
    )
    if own_service:
        parts.append("\n## Service architecture (direct deps only)\n")
        parts.append(_render_service_brief(own_service))
        for dep_name in own_service.depends_on:
            dep = next(
                (s for s in architecture.services if s.name == dep_name), None,
            )
            if dep:
                parts.append(_render_service_brief(dep))

    # Filtered capabilities (only what this issue delivers).
    relevant_caps = [
        c for c in enriched_spec.capabilities if c.id in set(issue.spec_refs)
    ]
    if relevant_caps:
        parts.append("\n## Capabilities this issue delivers\n")
        for cap in relevant_caps:
            parts.append(_render_capability(cap))

    if auth_contract:
        parts.append("\n## Auth contract\n")
        parts.append(
            "**This is your canonical FusionAuth reference.** Endpoints, "
            "test users, password rules, JWT validation — copy the EXACT "
            "shapes shown below when your code touches auth. Do NOT guess "
            "FA API paths from memory; the contract is the source of truth.\n\n"
        )
        parts.append("```markdown\n")
        parts.append(auth_contract.strip() + "\n")
        parts.append("```\n")

    # Framework conventions for the issue's service only.
    if own_service is not None:
        # Build a 1-service architecture stand-in for the renderer.
        from copy import copy
        single = copy(architecture)
        single.services = [own_service]
        fw = render_for_engineer(single)
        if fw:
            parts.append("\n" + fw)

    if skeleton_md:
        parts.append("\n## SKELETON.md (conventions for this skeleton)\n")
        parts.append(
            "**This is your service's structural contract.** Read the "
            "extension points and conventions before adding files. "
            "Files outside the declared extension points are dead code "
            "in the running container.\n\n"
        )
        parts.append("```markdown\n")
        parts.append(skeleton_md.strip() + "\n")
        parts.append("```\n")

    if workspace_summary:
        parts.append("\n## Workspace tree (for orientation)\n")
        parts.append("```\n")
        parts.append(workspace_summary.strip() + "\n")
        parts.append("```\n")

    parts.append(
        "\n## Your task\n"
        "1. Discover (view_file the target_files + 1-2 deps)\n"
        "2. Write code (write_file each target_file)\n"
        "3. validate_symbols — REQUIRED. Fix anything flagged.\n"
        "4. Write tests (write_file each test_file)\n"
        "5. run_tests. Iterate on failures (quick-pass + 1 retry,\n"
        "   then grind).\n"
        "6. submit_code when tests pass.\n"
    )
    return "".join(parts)


def _render_service_brief(svc: ServiceDefinition) -> str:
    deps = ", ".join(svc.depends_on) if svc.depends_on else "—"
    return (
        f"  - **{svc.name}** ({svc.service_type}/{svc.framework}, "
        f"{svc.language}, port {svc.port}, depends_on: {deps}): "
        f"{svc.description}\n"
    )


def _render_capability(cap: CapabilitySpec) -> str:
    lines: List[str] = [f"\n### `{cap.id}` — {cap.name}\n"]
    lines.append(f"{cap.description}\n")
    if cap.inputs:
        lines.append("\n**Inputs:**\n")
        for f in cap.inputs:
            req = "required" if f.required else "optional"
            constraints = (
                "; ".join(f.constraints) if f.constraints else "no constraints"
            )
            lines.append(
                f"  - `{f.name}` ({f.type}, {req}): {constraints}"
                + (f" — {f.description}" if f.description else "") + "\n"
            )
    if cap.outputs:
        lines.append("\n**Outputs:**\n")
        for f in cap.outputs:
            lines.append(f"  - `{f.name}` ({f.type})\n")
    if cap.validation_rules:
        lines.append("\n**Validation:**\n")
        for r in cap.validation_rules:
            lines.append(f"  - {r}\n")
    if cap.error_cases:
        lines.append("\n**Error cases:**\n")
        for e in cap.error_cases:
            lines.append(f"  - {e}\n")
    if cap.edge_cases:
        lines.append("\n**Edge cases:**\n")
        for e in cap.edge_cases:
            lines.append(f"  - {e}\n")
    if cap.test_scenarios:
        lines.append("\n**Test scenarios:**\n")
        for t in cap.test_scenarios:
            lines.append(f"  - {t}\n")
    if cap.auth_required:
        roles = ", ".join(cap.allowed_roles) if cap.allowed_roles else "any auth'd user"
        lines.append(f"\n**Auth:** required, roles: {roles}\n")
    return "".join(lines)
