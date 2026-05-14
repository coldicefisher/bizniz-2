"""QualityEngineer — preflight enrichment + post-flight test review.

Single-call dual-mode agent. Two methods on one class:

  - ``enrich(milestone, architecture, auth_contract, prior_contracts)``
        Produces an ``EnrichedSpec``: capabilities, validation rules,
        error cases, test scenarios, anti-patterns. Runs BEFORE the
        Engineer so the Engineer plans against a production-grade spec
        instead of inferring requirements from the milestone alone.

  - ``review(milestone, enriched_spec, engineer_plan, test_files)``
        Returns a ``CoverageReport``: did the tests cover the spec?
        Where are the gaps? Bias firewall: the reviewer never sees
        source code. Comparing tests-to-spec keeps it honest.

Both are single-shot LLM calls — no tool loop, no iterative discovery.
The Engineer between them does the heavy work.
"""
from bizniz.quality_engineer.agent import QualityEngineer
from bizniz.quality_engineer.types import (
    CapabilitySpec,
    CoverageReport,
    CoverageVerdict,
    EnrichedSpec,
    Field,
    MissingScenario,
    QualityEngineerError,
)

__all__ = [
    "QualityEngineer",
    "EnrichedSpec",
    "CapabilitySpec",
    "Field",
    "CoverageReport",
    "CoverageVerdict",
    "MissingScenario",
    "QualityEngineerError",
]
