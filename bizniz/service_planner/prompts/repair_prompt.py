"""ServicePlanner repair-mode user prompt.

Same shape as the implement-mode prompt, but seeded with:
  - The list of issues already attempted in this service + their
    dispositions from the IssueStateStore (so the planner knows what
    was tried and what failed).
  - The post-flight review findings scoped to this service:
      * QualityEngineer.CoverageReport (capability gaps, missing
        scenarios, recommendations)
      * CodeReviewer.CodeReviewReport (flagged symbols, anti-pattern
        violations, ungated auth, missing error handling)

Output: a list of NEW Issue objects whose target_files / test_files
overlap the originals BUT whose ids are unique repair ids
(e.g. ``BE-001-fix1``). The Coder runs them as normal issues; the
description has a ``# REPAIR NOTES`` section telling the Coder
exactly what to fix.
"""
from __future__ import annotations

from typing import List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.coder.types import Issue
from bizniz.lib.framework_conventions import render_for_engineer
from bizniz.quality_engineer.types import CoverageReport, EnrichedSpec


def build_repair_prompt(
    *,
    architecture: SystemArchitecture,
    enriched_spec: EnrichedSpec,
    service: ServiceDefinition,
    prior_issues: List[Issue],
    prior_dispositions: dict,            # issue_id → disposition
    coverage_report: Optional[CoverageReport],
    code_review_report: Optional[CodeReviewReport],
    repair_iteration: int,                # 1 or 2
    skeleton_md: Optional[str] = None,
    auth_contract: Optional[str] = None,
) -> str:
    """Assemble the user prompt for ServicePlanner.plan_repair()."""
    sections: list[str] = []

    sections.append(
        f"## Repair iteration {repair_iteration} for service "
        f"`{service.name}`\n"
        f"The previous implementation passed engineering but the "
        f"post-flight review surfaced critical findings. Your job is "
        f"to plan the MINIMUM set of fix-issues that address the "
        f"findings — do NOT re-plan the whole service from scratch."
    )

    sections.append(f"## Target service\n\n{_render_service(service)}")

    sections.append("## Prior implementation issues + dispositions\n")
    if not prior_issues:
        sections.append("(no prior issues recorded — unusual; treat as fresh plan)")
    else:
        lines = []
        for issue in prior_issues:
            disp = prior_dispositions.get(issue.id, "unknown")
            lines.append(
                f"- **{issue.id}** [{disp}]: {issue.title}\n"
                f"  - target_files: {', '.join(issue.target_files) or '(none)'}\n"
                f"  - spec_refs: {', '.join(issue.spec_refs) or '(none)'}"
            )
        sections.append("\n".join(lines))

    if coverage_report and (
        coverage_report.coverage_by_capability
        or coverage_report.missing_scenarios
        or coverage_report.recommendations
    ):
        sections.append("## QualityEngineer findings (test coverage)\n")
        sections.append(_render_coverage(coverage_report))

    if code_review_report and (
        code_review_report.flagged_symbols
        or code_review_report.anti_pattern_violations
        or code_review_report.ungated_auth
        or code_review_report.missing_error_handling
    ):
        sections.append("## CodeReviewer findings (source code)\n")
        sections.append(_render_code_review(code_review_report, service))

    sections.append("## Capabilities (for spec_ref lookups)\n")
    sections.append(_render_capabilities_compact(enriched_spec))

    fw = render_for_engineer(architecture)
    if fw:
        sections.append(fw)

    if skeleton_md:
        sections.append(
            "## Skeleton directory contract (still applies)\n\n"
            f"{skeleton_md}"
        )

    if auth_contract:
        sections.append(
            "## Auth contract (still applies)\n\n"
            f"{auth_contract}"
        )

    sections.append(
        "## Your job\n\n"
        f"Emit ONLY fix-issues that address the findings above. Each "
        f"fix-issue:\n"
        f"  - id: `<original-issue-id>-fix{repair_iteration}` (e.g. "
        f"`BE-001-fix{repair_iteration}`). If the fix spans multiple "
        f"original issues or doesn't map to one, use "
        f"`{service.name.upper()[:2]}-fix{repair_iteration}-N` "
        f"(e.g. `BA-fix{repair_iteration}-1`).\n"
        f"  - description MUST start with a `# REPAIR NOTES` section "
        f"that quotes the specific findings being fixed (capability "
        f"id / file / line / symbol / scenario). The Coder uses this "
        f"to know what to change.\n"
        f"  - target_files: the files that need to change. Often the "
        f"same as the original issue; sometimes adjacent files.\n"
        f"  - test_files: keep the original's, OR add a NEW test file "
        f"for missing scenarios.\n"
        f"  - depends_on: empty if the fix is independent. Use prior "
        f"fix-issue ids if one fix must land before another.\n"
        f"  - spec_refs: the capability ids the fix delivers (often "
        f"the same as the original).\n\n"
        f"Aim for as few fix-issues as possible — ideally 1-3. Do NOT "
        f"re-create issues that previously passed and aren't in the "
        f"findings.\n\n"
        f"Return ONE JSON object matching the schema."
    )

    return "\n\n".join(sections)


# ── Helpers ────────────────────────────────────────────────────────────


def _render_service(service: ServiceDefinition) -> str:
    deps = ", ".join(service.depends_on) if service.depends_on else "—"
    lines = [
        f"- name: `{service.name}`",
        f"- type: {service.service_type}",
        f"- framework: {service.framework}",
        f"- language: {service.language}",
        f"- workspace: `{service.workspace_name}/`",
        f"- depends_on: {deps}",
    ]
    if service.skeleton:
        lines.append(f"- skeleton: {service.skeleton}")
    return "\n".join(lines)


def _render_coverage(report: CoverageReport) -> str:
    lines: list[str] = []
    missing = [k for k, v in report.coverage_by_capability.items()
               if v in ("missing", "partial")]
    if missing:
        lines.append("**Capability gaps:**")
        for cap_id in missing:
            verdict = report.coverage_by_capability[cap_id]
            lines.append(f"  - `{cap_id}` — {verdict}")
        lines.append("")
    if report.missing_scenarios:
        lines.append("**Missing scenarios:**")
        for ms in report.missing_scenarios:
            lines.append(
                f"  - [{ms.priority}] `{ms.capability_id}`: {ms.scenario}"
            )
        lines.append("")
    if report.recommendations:
        lines.append("**Recommendations:**")
        for r in report.recommendations:
            lines.append(f"  - {r}")
    return "\n".join(lines)


def _render_code_review(
    report: CodeReviewReport,
    service: ServiceDefinition,
) -> str:
    """Filter findings by service workspace prefix when possible."""
    lines: list[str] = []

    workspace_prefix = (service.workspace_name or "") + "/"

    def _in_service(file_path: str) -> bool:
        # If the path contains the workspace name, it's this service's.
        # If it has no recognizable workspace prefix, include it (better
        # to over-include than miss).
        if not file_path:
            return True
        return workspace_prefix in file_path or "/" not in file_path

    flagged = [f for f in report.flagged_symbols
               if f.severity == "critical" and _in_service(f.file)]
    if flagged:
        lines.append("**Hallucinated / unresolved symbols:**")
        for f in flagged:
            lines.append(
                f"  - `{f.file}:{f.line}` [{f.kind}] `{f.symbol}` — {f.reason}"
            )
        lines.append("")

    anti = [a for a in report.anti_pattern_violations
            if a.severity == "critical" and _in_service(a.file)]
    if anti:
        lines.append("**Anti-pattern violations:**")
        for a in anti:
            lines.append(
                f"  - `{a.file}:{a.line}` violates: {a.anti_pattern}"
            )
            if a.evidence:
                lines.append(f"    evidence: `{a.evidence[:80]}`")
        lines.append("")

    ungated = [u for u in report.ungated_auth
               if u.severity == "critical" and _in_service(u.file)]
    if ungated:
        lines.append("**Ungated auth (capability exposed without role check):**")
        for u in ungated:
            lines.append(
                f"  - `{u.file}` capability `{u.capability_id}` — {u.evidence}"
            )
        lines.append("")

    missing_eh = [m for m in report.missing_error_handling
                  if _in_service(m.file)]
    if missing_eh:
        lines.append("**Missing error handling:**")
        for m in missing_eh:
            lines.append(
                f"  - `{m.file or '(unknown)'}` capability `{m.capability_id}` "
                f"missing case: {m.error_case}"
            )
        lines.append("")

    if report.recommendations:
        lines.append("**Reviewer recommendations:**")
        for r in report.recommendations:
            lines.append(f"  - {r}")

    if not lines:
        return "(no findings scoped to this service)"
    return "\n".join(lines)


def _render_capabilities_compact(spec: EnrichedSpec) -> str:
    """Compact one-liner per capability — repair planner doesn't need
    full input/output catalogs, just enough to map spec_refs."""
    if not spec.capabilities:
        return "(none)"
    return "\n".join(
        f"  - `{c.id}` — {c.name}: {c.description[:120]}"
        for c in spec.capabilities
    )
