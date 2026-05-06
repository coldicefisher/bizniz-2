"""CodeReviewer — fresh-context post-flight code review.

Single-call agent. Reads the Engineer's source code with NO Engineer
chat history, against the EnrichedSpec + AUTH_CONTRACT + prior contracts.
Flags hallucinations (fabricated symbols/types/imports), anti-pattern
violations from the spec, ungated auth, and missing error handling.

Distinct from QualityEngineer: QE.review has a bias firewall against
source code (tests + spec only). CodeReviewer's contract is the
opposite — it ALWAYS sees source, with fresh context, like a human
reviewer reading a PR cold.

Replaces ``bizniz/integration/hallucination_guard.py`` (heuristic
word-matcher that only caught domain-noun leakage). The judgment-based
approach catches fabricated function/type/field names too, which the
old guard could not.
"""
from bizniz.code_reviewer.agent import CodeReviewer
from bizniz.code_reviewer.types import (
    AntiPatternViolation,
    CodeReviewError,
    CodeReviewReport,
    FlaggedSymbol,
    MissingErrorHandling,
    UngatedAuthCapability,
)

__all__ = [
    "CodeReviewer",
    "CodeReviewReport",
    "FlaggedSymbol",
    "AntiPatternViolation",
    "UngatedAuthCapability",
    "MissingErrorHandling",
    "CodeReviewError",
]
