"""Orchestrator result types."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from bizniz.coder.types import CoderResult


IssueDisposition = Literal[
    "passed",       # Coder returned status=passed
    "partial",      # Coder returned status=partial (code written, tests red)
    "stalled",      # All tiers exhausted on stall
    "escalated",    # Passed only after escalating to a higher tier
    "errored",      # Unexpected exception
    "skipped",      # Dependency previously failed
]


class IssueOutcome(BaseModel):
    """One issue's lifecycle through the orchestrator."""
    issue_id: str
    disposition: IssueDisposition
    tiers_used: List[str] = Field(
        default_factory=list,
        description="Model names tried for this issue, in order.",
    )
    final_result: Optional[CoderResult] = None
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.disposition in ("passed", "escalated")


class OrchestratorResult(BaseModel):
    """Aggregate result for one orchestrator.run_service() call."""
    service: str
    issues: List[IssueOutcome] = Field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(o.passed for o in self.issues)

    @property
    def passed_count(self) -> int:
        return sum(1 for o in self.issues if o.passed)
