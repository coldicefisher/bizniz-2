"""QualityEngineer data model.

Two LLM-produced types:
  - ``EnrichedSpec`` from ``enrich()`` — what the Engineer builds against
  - ``CoverageReport`` from ``review()`` — verdict on the tests

Both are Pydantic models so we get free schema generation, validation,
and JSON round-tripping. Conservative field set — anything we can't
clearly justify isn't here. Add fields when a real consumer needs them.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field as PydField


class QualityEngineerError(Exception):
    """The QualityEngineer's LLM call returned bad / missing data."""


# ── Enrich output ──────────────────────────────────────────────────────


class Field(BaseModel):
    """One input or output field on a capability."""
    name: str
    type: str = PydField(
        ..., description="Plain-English type: 'string', 'uuid', 'integer', 'datetime', 'enum:active|paused', 'list[string]', etc."
    )
    required: bool = True
    constraints: List[str] = PydField(
        default_factory=list,
        description="Free-text rules the Engineer should enforce: 'min length 1', 'must be unique', 'E.164', etc.",
    )
    description: str = ""


class CapabilitySpec(BaseModel):
    """One discrete behavior the Engineer must deliver.

    ``id`` is a stable identifier the Engineer threads back via
    ``Issue.spec_refs`` and the Reviewer matches against test files.
    Use ``snake_case`` (e.g. ``create_pet``, ``list_owners_by_status``).
    """
    id: str = PydField(..., description="Stable snake_case identifier.")
    name: str
    description: str
    inputs: List[Field] = PydField(default_factory=list)
    outputs: List[Field] = PydField(default_factory=list)
    validation_rules: List[str] = PydField(default_factory=list)
    error_cases: List[str] = PydField(
        default_factory=list,
        description="Plain-English: 'duplicate email → 409 Conflict with {detail}'.",
    )
    edge_cases: List[str] = PydField(default_factory=list)
    auth_required: bool = True
    allowed_roles: List[str] = PydField(
        default_factory=list,
        description="If empty + auth_required, all authenticated users.",
    )
    test_scenarios: List[str] = PydField(
        default_factory=list,
        description="Named test ideas the Engineer/tester should cover.",
    )


class EnrichedSpec(BaseModel):
    """The full pre-flight enrichment of a milestone.

    Threaded into the Engineer's initial context. The Engineer's
    ``submit_plan`` issues should reference these capability ids in
    ``spec_refs`` so the post-flight reviewer can match coverage.
    """
    milestone_name: str
    capabilities: List[CapabilitySpec]
    cross_cutting: Dict[str, List[str]] = PydField(
        default_factory=dict,
        description="Concerns spanning multiple capabilities: 'error_handling', 'validation', 'logging', 'auth'.",
    )
    anti_patterns: List[str] = PydField(
        default_factory=list,
        description="Things production code MUST NOT do (e.g. 'never log raw passwords').",
    )
    confidence: float = PydField(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="QE's self-assessed confidence in the enrichment (0-1).",
    )


# ── Review output ──────────────────────────────────────────────────────


CoverageVerdict = Literal["covered", "partial", "missing"]


class MissingScenario(BaseModel):
    capability_id: str
    scenario: str
    priority: Literal["critical", "important", "nice-to-have"] = "important"


class CoverageReport(BaseModel):
    """Post-flight verdict on the Engineer's tests vs the EnrichedSpec.

    The reviewer never sees source code — only tests + spec. ``approved``
    is the gating bit; pipeline can refuse to merge a milestone if the
    review comes back un-approved.
    """
    milestone_name: str
    approved: bool
    coverage_by_capability: Dict[str, CoverageVerdict] = PydField(
        default_factory=dict,
        description="capability_id → covered/partial/missing.",
    )
    missing_scenarios: List[MissingScenario] = PydField(default_factory=list)
    recommendations: List[str] = PydField(
        default_factory=list,
        description="Specific actions the Engineer should take to reach approval.",
    )
    bias_check_passed: bool = PydField(
        default=True,
        description="True if the reviewer is confident no source code leaked into its inputs.",
    )
    summary: str = ""
    confidence: float = PydField(default=1.0, ge=0.0, le=1.0)

    @property
    def covered_count(self) -> int:
        return sum(1 for v in self.coverage_by_capability.values() if v == "covered")

    @property
    def total_count(self) -> int:
        return len(self.coverage_by_capability)

    def coverage_ratio(self) -> float:
        return self.covered_count / self.total_count if self.total_count else 0.0


# ── Patch output ───────────────────────────────────────────────────────


class QETestPatch(BaseModel):
    """A test file the QE writes inline to cover a gap it identified.

    path is workspace-relative (same convention as seeded scaffold).
    content is the complete file — write it verbatim to disk.
    capability_ids links back to CanonicalFinding ids so the harness
    can mark the right findings auto-resolved when the patch validates.
    """
    path: str
    content: str
    capability_ids: List[str] = PydField(
        default_factory=list,
        description="Capability IDs from the EnrichedSpec this patch covers.",
    )
    scenario_descriptions: List[str] = PydField(
        default_factory=list,
        description="Human-readable list of scenarios covered, for logging.",
    )


class QEPatchResult(BaseModel):
    """Returned by QualityEngineer.patch() — a batch of test file patches."""
    patches: List[QETestPatch] = PydField(default_factory=list)
