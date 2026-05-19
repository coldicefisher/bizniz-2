"""Unified findings shape for the v3 review unit.

Today's pipeline has three separate review loops (QE, CR, pytest)
running sequentially. The v3 spec replaces them with a single
parallel fan-out + a unified findings report + a batch-fix
debugger that consumes the whole report at once.

This module defines the shape ALL signal sources converge to. Each
source (mypy, ruff, tsc, pytest, QualityEngineer, CodeReviewer)
emits ``UnifiedFinding`` entries into a ``FindingsReport`` that the
debugger reads as one input.

Severity is normalized across sources so the debugger can prioritize
without knowing each tool's native severity vocabulary:

  - ``critical``: would block ship (security, broken contract, halt)
  - ``high``:     real bug (test failure, type error, hallucinated symbol)
  - ``medium``:   meaningful flaw (missing coverage, missing error handling)
  - ``low``:      style/nit (lint warnings, unused imports)

Source-of-truth: when a tool produces vague severity, map UP one
tier on doubt (don't drop signal).
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ── Severity + source enums ───────────────────────────────────────


Severity = Literal["critical", "high", "medium", "low"]

# Source identifies which tool produced the finding. Lets the debugger
# weigh trust (e.g., AST-level reports are deterministic and rarely
# wrong; QE/CR are LLM-judgment-based and noisier).
Source = Literal[
    "static_ast",       # AST parse / syntax errors
    "static_symbol",    # symbol_validator
    "static_mypy",      # type checker
    "static_ruff",      # lint
    "static_tsc",       # TypeScript compile
    "pytest",           # actual test run
    "quality_engineer", # QE.review coverage gaps
    "code_reviewer",    # CR critical findings
    "hallucination",    # hallucination-check agent
]


# ── Unified finding ───────────────────────────────────────────────


class UnifiedFinding(BaseModel):
    """One actionable issue, normalized across all signal sources.

    Two findings are equivalent (and should dedupe) when they share
    ``(file_path, line, source, fingerprint)``. ``fingerprint`` is a
    short hash-friendly tag the source emits (e.g. mypy error code
    ``arg-type``, ruff rule ``F401``, pytest test id).
    """

    source: Source = Field(
        ...,
        description="Which tool produced this finding.",
    )
    severity: Severity = Field(
        ...,
        description="Normalized severity for cross-source prioritization.",
    )
    fingerprint: str = Field(
        ...,
        description=(
            "Short tool-specific tag for dedup + reproducibility. "
            "Examples: 'TS6133' (tsc), 'F401' (ruff), 'arg-type' (mypy), "
            "'test_me::test_missing_auth' (pytest), 'cap.local_user_mirror' "
            "(QE), 'fake-import' (hallucination)."
        ),
    )
    message: str = Field(
        ...,
        description="One-line human-readable description of the issue.",
    )
    file_path: Optional[str] = Field(
        None,
        description="Workspace-relative path. None for project-wide findings.",
    )
    line: Optional[int] = Field(
        None,
        description="1-indexed line number when known.",
    )
    suggested_fix: Optional[str] = Field(
        None,
        description=(
            "Optional hint from the source about how to fix. The "
            "debugger may ignore this if it has a better idea, but "
            "tools like mypy + ruff often give precise fix suggestions."
        ),
    )
    raw: Optional[str] = Field(
        None,
        description=(
            "Source-native raw output for the finding (e.g., the mypy "
            "error line, the pytest traceback fragment). Kept for "
            "debugger context when ``message`` isn't enough."
        ),
    )


# ── Findings report ───────────────────────────────────────────────


class FindingsReport(BaseModel):
    """Snapshot of every finding across all signal sources for one
    iteration of the review unit.

    Two reports are comparable via ``count`` — the ProgressTracker
    uses (prior_count, current_count) to decide stall vs. progress.
    """

    iteration: int = Field(
        default=0,
        description=(
            "Which review-unit iteration produced this report. "
            "Iteration 0 is the first pass; ProgressTracker uses "
            "the count delta between successive iterations to decide "
            "stall vs. progress."
        ),
    )
    findings: List[UnifiedFinding] = Field(
        default_factory=list,
        description="Every unified finding in this iteration.",
    )

    @property
    def count(self) -> int:
        """Total findings count — the ProgressTracker's input."""
        return len(self.findings)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "high")

    def by_severity(self) -> dict:
        return {
            "critical": [f for f in self.findings if f.severity == "critical"],
            "high": [f for f in self.findings if f.severity == "high"],
            "medium": [f for f in self.findings if f.severity == "medium"],
            "low": [f for f in self.findings if f.severity == "low"],
        }

    def by_source(self) -> dict:
        out: dict = {}
        for f in self.findings:
            out.setdefault(f.source, []).append(f)
        return out

    def summary_line(self) -> str:
        s = self.by_severity()
        return (
            f"iter {self.iteration}: "
            f"{self.count} findings "
            f"({len(s['critical'])} critical, {len(s['high'])} high, "
            f"{len(s['medium'])} medium, {len(s['low'])} low)"
        )


# ── Progress signal ───────────────────────────────────────────────


class ProgressVerdict(BaseModel):
    """ProgressTracker output for one transition between iterations."""

    verdict: Literal["initial", "progress", "stall", "regress", "clean"] = Field(
        ...,
        description=(
            "initial = first iteration, no prior to compare against; "
            "progress = findings dropped; stall = same/similar count; "
            "regress = findings increased; clean = zero findings."
        ),
    )
    prior_count: int = 0
    current_count: int = 0
    stall_counter: int = 0
    stall_threshold: int = 5
    should_continue: bool = True
    should_escalate_tier: bool = False
