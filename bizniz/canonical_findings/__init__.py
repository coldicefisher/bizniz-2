"""``canonical_findings`` — v5 convergence machinery.

Reviewer runs ONCE per milestone (iter 1) and freezes its output as
a ``CanonicalReport``. Every subsequent iter runs a structured
*resolution check* against the frozen list — never inventing new
findings. Defect count becomes monotone non-increasing; convergence
is mathematically guaranteed as long as each iter resolves ≥ 1
finding (else stall).

This module: types, fingerprint, persistence. The agents (resolution
checker) and orchestration (v5 loop) live elsewhere.
"""
from bizniz.canonical_findings.fingerprint import canonical_fingerprint
from bizniz.canonical_findings.persistence import (
    load_canonical_report,
    save_canonical_report,
)
from bizniz.canonical_findings.types import (
    CanonicalFinding,
    CanonicalReport,
    FindingResolution,
    ResolutionReport,
    ResolutionStatus,
)

__all__ = [
    "CanonicalFinding",
    "CanonicalReport",
    "FindingResolution",
    "ResolutionReport",
    "ResolutionStatus",
    "canonical_fingerprint",
    "load_canonical_report",
    "save_canonical_report",
]
