"""Adapter: ``CodeReviewReport`` → ``List[UnifiedFinding]``.

Severity mapping:
  - CR's own ``critical`` severity      → UnifiedFinding ``critical``
  - CR's ``warning`` severity           → UnifiedFinding ``medium``

Four finding categories from CR map to UnifiedFinding source labels:
  - flagged_symbols          → source="code_reviewer", fingerprint=cr.symbol.<sym>
  - anti_pattern_violations  → source="code_reviewer", fingerprint=cr.anti.<rule>
  - ungated_auth             → source="code_reviewer", fingerprint=cr.auth.<cap>
  - missing_error_handling   → source="code_reviewer", fingerprint=cr.err.<cap>
"""
from __future__ import annotations

from typing import List

from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.review_unit.types import UnifiedFinding


def cr_report_to_findings(report: CodeReviewReport) -> List[UnifiedFinding]:
    """Convert a CodeReviewer CodeReviewReport into UnifiedFinding[]."""
    findings: List[UnifiedFinding] = []

    for fs in (report.flagged_symbols or []):
        sev = "critical" if fs.severity == "critical" else "medium"
        findings.append(UnifiedFinding(
            source="code_reviewer",
            severity=sev,
            fingerprint=f"cr.symbol.{fs.kind}.{fs.symbol}",
            message=(
                f"[{fs.kind}] `{fs.symbol}` flagged as possibly "
                f"fabricated: {fs.reason}"
            ),
            file_path=fs.file,
            line=fs.line or None,
        ))

    for ap in (report.anti_pattern_violations or []):
        sev = "critical" if ap.severity == "critical" else "medium"
        findings.append(UnifiedFinding(
            source="code_reviewer",
            severity=sev,
            fingerprint=f"cr.anti.{_short_hash(ap.anti_pattern)}",
            message=(
                f"Anti-pattern violation: {ap.anti_pattern}. "
                f"Evidence: {ap.evidence[:200]}"
            ),
            file_path=ap.file,
            line=ap.line or None,
        ))

    for au in (report.ungated_auth or []):
        sev = "critical" if au.severity == "critical" else "medium"
        findings.append(UnifiedFinding(
            source="code_reviewer",
            severity=sev,
            fingerprint=f"cr.auth.{au.capability_id}",
            message=(
                f"Capability `{au.capability_id}` exposed without "
                f"required auth gate. Evidence: {au.evidence[:200]}"
            ),
            file_path=au.file,
        ))

    for eh in (report.missing_error_handling or []):
        sev = "critical" if eh.severity == "critical" else "medium"
        findings.append(UnifiedFinding(
            source="code_reviewer",
            severity=sev,
            fingerprint=f"cr.err.{eh.capability_id}.{_short_hash(eh.error_case)}",
            message=(
                f"Missing error handling for `{eh.capability_id}`: "
                f"{eh.error_case}"
            ),
            file_path=eh.file or None,
        ))

    return findings


def _short_hash(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]
