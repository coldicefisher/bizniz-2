"""Adapter: ``CoverageReport`` (QualityEngineer.review output) →
``List[UnifiedFinding]``.

Severity mapping:
  - missing_scenarios with priority="critical"   → severity=critical
  - missing_scenarios with priority="important"  → severity=high
  - missing_scenarios with priority="nice-to-have" → severity=medium
  - capabilities with coverage_by_capability="missing"  → high (top-level gap)
  - capabilities with coverage_by_capability="partial"  → medium
  - recommendations (free-form text)             → low (no fingerprint anchor)

Bias-check failures + un-approved verdict don't get separate findings
— they're already reflected in the missing_scenarios + coverage maps.
"""
from __future__ import annotations

from typing import List

from bizniz.quality_engineer.types import CoverageReport
from bizniz.review_unit.types import UnifiedFinding


def qe_coverage_to_findings(report: CoverageReport) -> List[UnifiedFinding]:
    """Convert a QualityEngineer CoverageReport into UnifiedFinding[]."""
    findings: List[UnifiedFinding] = []

    # Per-capability coverage gaps.
    for cap_id, verdict in (report.coverage_by_capability or {}).items():
        if verdict == "missing":
            findings.append(UnifiedFinding(
                source="quality_engineer",
                severity="high",
                fingerprint=f"cap.{cap_id}.missing",
                message=(
                    f"Capability `{cap_id}` has no visible coverage in "
                    f"the test suite. The implementation may exist but "
                    f"isn't being exercised."
                ),
            ))
        elif verdict == "partial":
            findings.append(UnifiedFinding(
                source="quality_engineer",
                severity="medium",
                fingerprint=f"cap.{cap_id}.partial",
                message=(
                    f"Capability `{cap_id}` has partial test coverage — "
                    f"some scenarios exercised, others missing."
                ),
            ))

    # Per-missing-scenario findings carry priority → severity mapping.
    for ms in (report.missing_scenarios or []):
        sev = _priority_to_severity(ms.priority)
        findings.append(UnifiedFinding(
            source="quality_engineer",
            severity=sev,
            fingerprint=f"scenario.{ms.capability_id}.{_short_hash(ms.scenario)}",
            message=ms.scenario,
            raw=f"capability_id={ms.capability_id}; priority={ms.priority}",
        ))

    # Free-form recommendations — low severity, no anchor.
    for i, rec in enumerate(report.recommendations or []):
        findings.append(UnifiedFinding(
            source="quality_engineer",
            severity="low",
            fingerprint=f"qe.recommendation.{i}",
            message=rec,
        ))

    return findings


def _priority_to_severity(p: str) -> str:
    if p == "critical":
        return "critical"
    if p == "important":
        return "high"
    return "medium"


def _short_hash(s: str) -> str:
    """Short stable identifier for a scenario string (for fingerprinting).
    Not cryptographic — just dedup-friendly."""
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]
