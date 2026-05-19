"""``per_issue_validator`` data types."""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field


FindingSource = Literal[
    "symbol_validator",   # unresolved imports / attributes
    "ast",                # syntax errors caught while parsing
    "pytest_collect",     # pytest --collect-only failures
]


class Finding(BaseModel):
    """One defect surfaced by a deterministic scanner."""
    source: FindingSource
    file: str = ""
    line: int = 0
    message: str
    raw: str = Field(default="", description="Raw scanner output for context.")


class ValidatedIssue(BaseModel):
    """Result of one issue's validation pipeline.

    ``clean=True`` means every deterministic gate passed AND the
    files were written to disk. ``clean=False`` means scanners
    surfaced findings the agent couldn't drive to zero within the
    debug-iteration budget.

    Either way, the files ARE on disk (the v4 contract is that an
    issue ships its best-effort artifact; downstream review +
    cross-issue integration testing catches what per-issue
    validation can't).
    """
    issue_id: str
    clean: bool
    files_written: List[str] = Field(default_factory=list)
    findings: List[Finding] = Field(default_factory=list)
    debug_iterations: int = Field(
        default=0,
        description=(
            "How many extra agent invocations were spent driving "
            "findings toward zero. 0 = clean on first pass."
        ),
    )
    halt_reason: str = Field(
        default="",
        description=(
            "When clean=False, why the loop bailed: 'stall' "
            "(no progress), 'agent_error', or 'hard_cap'."
        ),
    )
