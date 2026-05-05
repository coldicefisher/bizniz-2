"""Typed output for the Issue Enrichment Agent.

The agent emits a structured ``EnrichedIssue`` per Engineer-emitted
issue. Sections are optional so an agent can skip dimensions that
don't apply (a frontend route ticket has empty ``auth_requirements``;
a pure-data domain model has empty ``test_scenarios`` for now).

Keeping output structured (vs free-text "production notes") gives
two wins:
  1. Coder's prompt can list required_fields verbatim — unambiguous.
  2. Downstream code can validate "did Coder produce every required
     field" against the structure.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class FieldSpec(BaseModel):
    """One field on a domain model, schema, or form."""
    name: str
    type: str = Field(
        description="Type as the language expresses it: 'str', 'int', "
                    "'Optional[str]', 'list[Photo]' for Python; 'string', "
                    "'number', 'string | null' for TypeScript.",
    )
    required: bool = True
    description: str = ""
    constraints: Optional[str] = Field(
        default=None,
        description="Validation expression: 'min_length=1, max_length=255', "
                    "'ge=1, le=1000', or 'must match RFC 5322'.",
    )


class ErrorCase(BaseModel):
    """One named error condition the implementation must handle."""
    status_code: int = Field(description="HTTP status (or analogous)")
    when: str = Field(
        description="Trigger condition in plain English: "
                    "'requested resource id not found', 'caller not the owner'.",
    )
    detail: Optional[str] = Field(
        default=None,
        description="Suggested error message body, if a specific format is "
                    "implied by the spec.",
    )


class EnrichedIssue(BaseModel):
    """Production-grade enrichment of an Engineer-emitted issue.

    The original issue's title/description are kept verbatim for
    traceability. Every other section is derived by the enrichment
    agent from the issue + technology stack + problem statement +
    auth contract + workspace context.

    Sections that don't apply to a given ticket should be left as
    their default empty values — the Coder ignores empty sections.
    """
    original_issue_title: str
    original_issue_description: str

    required_fields: List[FieldSpec] = Field(
        default_factory=list,
        description="Fields the implementation MUST include. Sourced from "
                    "the problem statement's noun list for this entity.",
    )
    optional_fields: List[FieldSpec] = Field(
        default_factory=list,
        description="Fields with sensible defaults or that the spec marks "
                    "as 'optional', 'may have', etc.",
    )
    validation_rules: List[str] = Field(
        default_factory=list,
        description="Cross-field or format-level rules that don't fit on "
                    "a single field's `constraints`.",
    )
    auth_requirements: List[str] = Field(
        default_factory=list,
        description="Auth-related decorations: 'Depends(get_current_user)', "
                    "'require_roles(\"landlord\")', 'no auth required'.",
    )
    error_cases: List[ErrorCase] = Field(
        default_factory=list,
        description="Named error conditions the implementation must handle.",
    )
    edge_cases: List[str] = Field(
        default_factory=list,
        description="Specific behaviors to exercise: empty result, "
                    "duplicate, race conditions named in the spec.",
    )
    test_scenarios: List[str] = Field(
        default_factory=list,
        description="High-level test cases the issue's tests should cover. "
                    "Names — actual test code is the Coder's job.",
    )
    dependencies_on_other_issues: List[str] = Field(
        default_factory=list,
        description="Issues by title that must complete before this one. "
                    "May be the same as the Engineer's depends_on, or "
                    "additional ones the agent identified.",
    )
    notes: List[str] = Field(
        default_factory=list,
        description="Catch-all for things that don't fit the structured "
                    "fields: implementation hints, gotchas, references "
                    "to existing patterns in the codebase.",
    )

    confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="High = enrichment is directly grounded in spec/contracts. "
                    "Medium = standard production-grade additions inferred from "
                    "the tech stack. Low = guesses; treat with skepticism.",
    )

    def is_empty(self) -> bool:
        """True if no enrichment dimensions were populated. The Engineer's
        original issue is enough for the Coder; no extra context to inject.
        """
        return not (
            self.required_fields
            or self.optional_fields
            or self.validation_rules
            or self.auth_requirements
            or self.error_cases
            or self.edge_cases
            or self.test_scenarios
            or self.dependencies_on_other_issues
            or self.notes
        )

    def to_coder_prompt_section(self) -> str:
        """Render the enrichment as a markdown section the Coder's prompt
        can include verbatim. Sections only render when populated, so the
        section stays short for thin tickets.
        """
        if self.is_empty():
            return ""

        out: List[str] = ["", "## Enriched ticket details", ""]

        if self.required_fields:
            out.append("### Required fields")
            for f in self.required_fields:
                cons = f" ({f.constraints})" if f.constraints else ""
                out.append(f"- **{f.name}**: `{f.type}`{cons} — {f.description}")
            out.append("")

        if self.optional_fields:
            out.append("### Optional fields")
            for f in self.optional_fields:
                cons = f" ({f.constraints})" if f.constraints else ""
                out.append(f"- **{f.name}**: `{f.type}`{cons} — {f.description}")
            out.append("")

        if self.validation_rules:
            out.append("### Validation rules")
            for r in self.validation_rules:
                out.append(f"- {r}")
            out.append("")

        if self.auth_requirements:
            out.append("### Auth requirements")
            for a in self.auth_requirements:
                out.append(f"- {a}")
            out.append("")

        if self.error_cases:
            out.append("### Error cases")
            for e in self.error_cases:
                detail = f" — {e.detail}" if e.detail else ""
                out.append(f"- **{e.status_code}** when {e.when}{detail}")
            out.append("")

        if self.edge_cases:
            out.append("### Edge cases to handle")
            for ec in self.edge_cases:
                out.append(f"- {ec}")
            out.append("")

        if self.test_scenarios:
            out.append("### Test scenarios")
            for t in self.test_scenarios:
                out.append(f"- {t}")
            out.append("")

        if self.notes:
            out.append("### Implementation notes")
            for n in self.notes:
                out.append(f"- {n}")
            out.append("")

        out.append(f"_Enrichment confidence: {self.confidence}_")
        return "\n".join(out)
