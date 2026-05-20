"""``resolution_checker`` — v5 iter-2+ reviewer.

At iter 1 the full QE+CR review runs; its findings get frozen as a
``CanonicalReport``. At iter 2+, this module runs the
``ResolutionChecker``: for each known canonical finding, examine
current code and emit a status (``resolved`` | ``still_present`` |
``regressed``). Cannot invent new findings.

The checker is per-source (QE flavor + CR flavor) and runs them in
parallel via ThreadPoolExecutor — same fan-out pattern as v3.1's
parallel review.
"""
from bizniz.resolution_checker.adapters import (
    cr_report_to_canonical_findings,
    qe_coverage_to_canonical_findings,
)
from bizniz.resolution_checker.checker import (
    ResolutionChecker,
    ResolutionCheckerError,
)

__all__ = [
    "ResolutionChecker",
    "ResolutionCheckerError",
    "qe_coverage_to_canonical_findings",
    "cr_report_to_canonical_findings",
]
