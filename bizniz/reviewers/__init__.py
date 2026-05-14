"""Code reviewers — deterministic integrity checks on generated code.

Run after the engineer completes a service (alongside Phase 6
post-flight type-checking). Catches structural bugs that the
type-checker can't:

- Duplicate routes (same HTTP path registered twice → request
  routing is undefined or the second handler shadows the first).
- Duplicate functions / classes across files.
- Router include conflicts (auto-discovery + explicit
  ``app.include_router`` both registering the same router with
  different prefixes — the M1 path-doubling bug class).

Future checks will land here too: dead code, unused imports,
overly-broad exception handling, SOLID violations. Today the
priority is route duplication because we keep hitting it on
every M1 run.

NO AI CALLS in v1 — pure mechanical AST analysis. We can layer
an AI-assisted reviewer for fuzzy/semantic duplication later
(two functions doing the same thing under different names).
"""
from bizniz.reviewers.route_review import (
    RouteReview,
    RouteIssue,
    review_routes,
)
from bizniz.reviewers.hallucination_review import (
    HallucinationReport,
    SuspiciousFile,
    collect_changed_files,
    review_for_hallucinations,
)

__all__ = [
    "RouteReview",
    "RouteIssue",
    "review_routes",
    "HallucinationReport",
    "SuspiciousFile",
    "collect_changed_files",
    "review_for_hallucinations",
]
