"""Convert iter-1 review outputs (CoverageReport + CodeReviewReport)
into canonical findings.

These adapters run ONCE per milestone (at iter 1 of review/repair).
Their output is frozen into a CanonicalReport and never re-judged.
"""
from __future__ import annotations

from typing import List

from bizniz.canonical_findings.fingerprint import canonical_fingerprint
from bizniz.canonical_findings.types import (
    CanonicalFinding,
    FindingPriority,
)
from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.quality_engineer.types import CoverageReport


# QE priority strings → canonical priority. The QE emits
# "critical | important | nice-to-have"; we normalize.
_QE_PRIORITY_MAP = {
    "critical": "critical",
    "important": "important",
    "nice-to-have": "nice_to_have",
}


def qe_coverage_to_canonical_findings(
    report: CoverageReport,
) -> List[CanonicalFinding]:
    """One canonical finding per missing scenario."""
    out: List[CanonicalFinding] = []
    for ms in (report.missing_scenarios or []):
        priority: FindingPriority = _QE_PRIORITY_MAP.get(
            ms.priority, "important",
        )
        fp = canonical_fingerprint(
            source="quality_engineer",
            capability_id=ms.capability_id,
            shape={"scenario": ms.scenario, "kind": "missing_scenario"},
        )
        out.append(CanonicalFinding(
            id=fp,
            source="quality_engineer",
            priority=priority,
            capability_id=ms.capability_id,
            summary=f"missing scenario for `{ms.capability_id}`: {ms.scenario}",
            detail=ms.scenario,
            status="initial",
        ))
    # Capabilities marked "missing" with no specific scenario also
    # surface — they're broader gaps.
    for cap_id, verdict in (report.coverage_by_capability or {}).items():
        if verdict != "missing":
            continue
        # Skip if we already emitted a scenario-level finding for this cap.
        if any(f.capability_id == cap_id for f in out):
            continue
        fp = canonical_fingerprint(
            source="quality_engineer",
            capability_id=cap_id,
            shape={"kind": "capability_missing"},
        )
        out.append(CanonicalFinding(
            id=fp,
            source="quality_engineer",
            priority="important",
            capability_id=cap_id,
            summary=f"capability `{cap_id}` missing all coverage",
            detail=f"QE verdict: {verdict}",
            status="initial",
        ))
    return out


def cr_report_to_canonical_findings(
    report: CodeReviewReport,
) -> List[CanonicalFinding]:
    """One canonical finding per critical CR finding."""
    out: List[CanonicalFinding] = []
    # Flagged symbols (hallucinated imports, etc.)
    for fs in (report.flagged_symbols or []):
        if fs.severity != "critical":
            continue
        fp = canonical_fingerprint(
            source="code_reviewer", capability_id=None,
            shape={
                "kind": "flagged_symbol",
                "file": fs.file, "symbol": fs.symbol, "symbol_kind": fs.kind,
            },
        )
        out.append(CanonicalFinding(
            id=fp,
            source="code_reviewer",
            priority="critical",
            summary=f"hallucinated {fs.kind} `{fs.symbol}` in {fs.file}",
            file_hint=fs.file,
            detail=fs.reason,
            status="initial",
        ))
    # Anti-pattern violations.
    for ap in (report.anti_pattern_violations or []):
        if ap.severity != "critical":
            continue
        fp = canonical_fingerprint(
            source="code_reviewer", capability_id=None,
            shape={
                "kind": "anti_pattern",
                "file": ap.file, "rule": ap.anti_pattern,
            },
        )
        out.append(CanonicalFinding(
            id=fp,
            source="code_reviewer",
            priority="critical",
            summary=f"anti-pattern in {ap.file}: {ap.anti_pattern}",
            file_hint=ap.file,
            detail=ap.evidence,
            status="initial",
        ))
    # Ungated auth.
    for ua in (report.ungated_auth or []):
        if ua.severity != "critical":
            continue
        fp = canonical_fingerprint(
            source="code_reviewer", capability_id=ua.capability_id,
            shape={
                "kind": "ungated_auth",
                "file": ua.file, "capability": ua.capability_id,
            },
        )
        out.append(CanonicalFinding(
            id=fp,
            source="code_reviewer",
            priority="critical",
            capability_id=ua.capability_id,
            summary=f"ungated auth on `{ua.capability_id}` in {ua.file}",
            file_hint=ua.file,
            detail=ua.evidence,
            status="initial",
        ))
    # Missing error handling — typically warning, but if elevated, surface.
    for me in (report.missing_error_handling or []):
        if me.severity != "critical":
            continue
        fp = canonical_fingerprint(
            source="code_reviewer", capability_id=me.capability_id,
            shape={
                "kind": "missing_error_handling",
                "capability": me.capability_id, "case": me.error_case,
            },
        )
        out.append(CanonicalFinding(
            id=fp,
            source="code_reviewer",
            priority="critical",
            capability_id=me.capability_id,
            summary=(
                f"missing error handling for `{me.capability_id}`: "
                f"{me.error_case}"
            ),
            file_hint=me.file or None,
            detail=me.error_case,
            status="initial",
        ))
    return out
