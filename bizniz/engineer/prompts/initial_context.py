"""Builds the first user message for the Engineer (both modes).

Two builders:
  - ``build_engineer_initial_context``  implement mode (build a milestone
    from EnrichedSpec)
  - ``build_engineer_repair_context``   repair mode (fix CodeReviewReport
    findings against existing code)
"""
from __future__ import annotations

from typing import Iterable, Optional

from bizniz.architect.types import SystemArchitecture
from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.types import EnrichedSpec


def build_engineer_initial_context(
    *,
    milestone: Milestone,
    architecture: SystemArchitecture,
    enriched_spec: EnrichedSpec,
    auth_contract: Optional[str] = None,
    prior_specs: Optional[Iterable[EnrichedSpec]] = None,
    workspace_summary: Optional[str] = None,
) -> str:
    """Compose the Engineer's initial user message.

    ``workspace_summary`` (optional) is a pre-rendered file-tree text
    block. Provided when the caller has already pruned out
    framework-cache directories. The Engineer can also ask for it via
    ``get_workspace_tree``.
    """
    parts = [f"# Milestone: {milestone.name}\n"]

    parts.append("\n## Problem slice\n")
    parts.append(milestone.problem_slice.strip() + "\n")

    if milestone.use_cases:
        parts.append("\n## Use cases\n")
        for uc in milestone.use_cases:
            parts.append(f"- {uc}\n")

    if milestone.success_criteria:
        parts.append("\n## Milestone success criteria\n")
        for sc in milestone.success_criteria:
            parts.append(f"- {sc}\n")

    parts.append("\n## Architecture\n")
    parts.append(_render_architecture(architecture))

    parts.append("\n## EnrichedSpec (your build-against contract)\n")
    parts.append("```json\n")
    parts.append(enriched_spec.model_dump_json(indent=2) + "\n")
    parts.append("```\n")

    if auth_contract:
        parts.append("\n## Auth contract (authoritative)\n")
        parts.append("```markdown\n")
        parts.append(auth_contract.strip() + "\n")
        parts.append("```\n")

    prior_list = list(prior_specs or [])
    if prior_list:
        parts.append(
            "\n## Prior milestones' EnrichedSpecs "
            "(for naming consistency, do NOT re-implement)\n"
        )
        for i, s in enumerate(prior_list, 1):
            parts.append(f"\n### Prior spec {i}: {s.milestone_name}\n")
            parts.append("```json\n")
            parts.append(s.model_dump_json(indent=2) + "\n")
            parts.append("```\n")

    if workspace_summary:
        parts.append("\n## Workspace tree (pre-rendered)\n")
        parts.append("```\n")
        parts.append(workspace_summary.strip() + "\n")
        parts.append("```\n")

    parts.append(
        "\n## Your task\n"
        "1. Submit a plan (action=`submit_plan`) covering every capability\n"
        "   in the EnrichedSpec. Each issue must reference at least one\n"
        "   capability id in `spec_refs`.\n"
        "2. Implement, test, debug.\n"
        "3. Submit your final result via `submit_implementation`.\n"
    )

    return "".join(parts)


def _render_architecture(arch: SystemArchitecture) -> str:
    lines = [
        f"Project: {arch.project_name} ({arch.project_slug})",
        f"Description: {arch.description}",
        "",
        "Services:",
    ]
    for s in arch.services:
        deps = ", ".join(s.depends_on) if s.depends_on else "—"
        lines.append(
            f"  - {s.name} ({s.service_type}/{s.framework}, "
            f"{s.language}, port {s.port}, depends_on: {deps})"
        )
        if s.description:
            lines.append(f"      {s.description}")
    return "\n".join(lines) + "\n"


def build_engineer_repair_context(
    *,
    milestone: Milestone,
    architecture: SystemArchitecture,
    code_review_report: CodeReviewReport,
    enriched_spec: Optional[EnrichedSpec] = None,
    auth_contract: Optional[str] = None,
    prior_specs: Optional[Iterable[EnrichedSpec]] = None,
) -> str:
    """Compose the Engineer's initial user message in REPAIR mode.

    The CodeReviewReport's rendered markdown is the seed. The
    EnrichedSpec is included for context (so the Engineer remembers
    what the milestone is supposed to do) but the work is bounded by
    the report's findings.
    """
    parts = [f"# Repair: {milestone.name}\n"]

    parts.append("\n## Milestone problem slice (for context only)\n")
    parts.append(milestone.problem_slice.strip() + "\n")

    parts.append("\n## Architecture\n")
    parts.append(_render_architecture(architecture))

    parts.append("\n## Code Review Report (your repair task)\n")
    parts.append(code_review_report.render_for_repair())
    parts.append("\n")

    crit = len(code_review_report.critical_findings)
    total = code_review_report.total_findings
    parts.append(
        f"\n**{total} finding(s) total, {crit} critical.** "
        f"Critical findings block approval — address them first.\n"
    )

    if enriched_spec is not None:
        parts.append("\n## EnrichedSpec (for reference — the original spec)\n")
        parts.append("```json\n")
        parts.append(enriched_spec.model_dump_json(indent=2) + "\n")
        parts.append("```\n")

    if auth_contract:
        parts.append("\n## Auth contract (authoritative)\n")
        parts.append("```markdown\n")
        parts.append(auth_contract.strip() + "\n")
        parts.append("```\n")

    prior_list = list(prior_specs or [])
    if prior_list:
        parts.append("\n## Prior milestone EnrichedSpecs (for naming consistency)\n")
        for i, s in enumerate(prior_list, 1):
            parts.append(f"\n### Prior spec {i}: {s.milestone_name}\n")
            parts.append("```json\n")
            parts.append(s.model_dump_json(indent=2) + "\n")
            parts.append("```\n")

    parts.append(
        "\n## Your task\n"
        "1. Submit a plan (action=`submit_plan`) — one issue per finding\n"
        "   or per closely-related cluster. Each issue says which file\n"
        "   it modifies in `target_files`. `spec_refs` is OPTIONAL in\n"
        "   repair mode.\n"
        "2. Fix the findings — surgical edits only, critical first.\n"
        "3. Run smoke_import + run_tests to verify.\n"
        "4. Submit via `submit_implementation` with status reflecting\n"
        "   what was actually fixed.\n"
    )

    return "".join(parts)
