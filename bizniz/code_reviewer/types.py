"""CodeReviewer data model.

Output is one ``CodeReviewReport`` aggregating four kinds of findings:

  - ``flagged_symbols``         fabricated imports/functions/types/fields
  - ``anti_pattern_violations`` violations of EnrichedSpec.anti_patterns
  - ``ungated_auth``            capabilities that should require auth
                                 but the code doesn't enforce it
  - ``missing_error_handling``  EnrichedSpec error_cases the code
                                 doesn't handle

Plus a verdict (``approved``), human-readable ``summary``, and
``confidence``. The report is the seed context for a follow-up
``Engineer.repair()`` if approval fails.
"""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field as PydField


class CodeReviewError(Exception):
    """The CodeReviewer's LLM call returned bad / missing data."""


Severity = Literal["critical", "warning"]


class FlaggedSymbol(BaseModel):
    """A symbol the reviewer suspects is hallucinated.

    ``critical`` = imports/calls that will crash at runtime.
    ``warning`` = looks suspicious (e.g., naming inconsistency) but
    might resolve via framework magic — Engineer should re-check.
    """
    file: str
    line: int = PydField(default=0, ge=0)
    symbol: str
    kind: Literal[
        "import", "function_call", "attribute", "class", "type", "field",
    ]
    reason: str
    severity: Severity = "critical"


class AntiPatternViolation(BaseModel):
    """A violation of an EnrichedSpec.anti_patterns rule.

    ``anti_pattern`` should quote the rule from the spec (or close to
    it). ``evidence`` is the offending code line / pattern.
    """
    file: str
    line: int = PydField(default=0, ge=0)
    anti_pattern: str
    evidence: str
    severity: Severity = "critical"


class UngatedAuthCapability(BaseModel):
    """A spec capability that's exposed without the auth gate the spec
    demands."""
    file: str
    capability_id: str
    evidence: str
    severity: Severity = "critical"


class MissingErrorHandling(BaseModel):
    """An EnrichedSpec error_case the implementation does not handle."""
    file: str = ""
    capability_id: str
    error_case: str
    severity: Severity = "warning"


class CodeReviewReport(BaseModel):
    """Post-flight code review verdict.

    ``approved=False`` if any critical-severity finding exists. The
    Engineer's ``repair()`` consumes this report as its seed context.
    """
    milestone_name: str
    approved: bool
    flagged_symbols: List[FlaggedSymbol] = PydField(default_factory=list)
    anti_pattern_violations: List[AntiPatternViolation] = PydField(default_factory=list)
    ungated_auth: List[UngatedAuthCapability] = PydField(default_factory=list)
    missing_error_handling: List[MissingErrorHandling] = PydField(default_factory=list)
    recommendations: List[str] = PydField(default_factory=list)
    summary: str = ""
    confidence: float = PydField(default=1.0, ge=0.0, le=1.0)

    @property
    def critical_findings(self) -> List[BaseModel]:
        """Every critical-severity finding across all categories."""
        out: List[BaseModel] = []
        out.extend(f for f in self.flagged_symbols if f.severity == "critical")
        out.extend(f for f in self.anti_pattern_violations if f.severity == "critical")
        out.extend(f for f in self.ungated_auth if f.severity == "critical")
        out.extend(f for f in self.missing_error_handling if f.severity == "critical")
        return out

    @property
    def total_findings(self) -> int:
        return (
            len(self.flagged_symbols)
            + len(self.anti_pattern_violations)
            + len(self.ungated_auth)
            + len(self.missing_error_handling)
        )

    @property
    def has_critical(self) -> bool:
        return len(self.critical_findings) > 0

    def render_for_repair(self) -> str:
        """Render as human-readable markdown for use as Engineer.repair seed."""
        lines = [f"# Code Review: {self.milestone_name}"]
        lines.append(f"\n**Verdict:** {'APPROVED' if self.approved else 'CHANGES REQUESTED'}")
        if self.summary:
            lines.append(f"\n{self.summary}")

        if self.flagged_symbols:
            lines.append("\n## Flagged Symbols (suspected hallucinations)")
            for f in self.flagged_symbols:
                where = f"{f.file}:{f.line}" if f.line else f.file
                lines.append(
                    f"- **[{f.severity}]** `{f.symbol}` ({f.kind}) at "
                    f"{where} — {f.reason}"
                )

        if self.anti_pattern_violations:
            lines.append("\n## Anti-pattern Violations")
            for v in self.anti_pattern_violations:
                where = f"{v.file}:{v.line}" if v.line else v.file
                lines.append(
                    f"- **[{v.severity}]** {v.anti_pattern} at {where}\n"
                    f"  Evidence: `{v.evidence}`"
                )

        if self.ungated_auth:
            lines.append("\n## Ungated Auth")
            for u in self.ungated_auth:
                lines.append(
                    f"- **[{u.severity}]** capability `{u.capability_id}` in "
                    f"{u.file} — {u.evidence}"
                )

        if self.missing_error_handling:
            lines.append("\n## Missing Error Handling")
            for m in self.missing_error_handling:
                where = f" ({m.file})" if m.file else ""
                lines.append(
                    f"- **[{m.severity}]** capability `{m.capability_id}`{where}: "
                    f"{m.error_case}"
                )

        if self.recommendations:
            lines.append("\n## Recommendations")
            for r in self.recommendations:
                lines.append(f"- {r}")

        return "\n".join(lines)
