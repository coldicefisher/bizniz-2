"""Engineer data model.

  - ``Issue``         one discrete piece of work (target_files, test_files,
                      spec_refs back to EnrichedSpec capability ids)
  - ``EngineerPlan``  the full set of issues + a narrative approach
  - ``EngineerResult`` final terminal payload — plan + summary + status
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field as PydField


class EngineerError(Exception):
    """Top-level Engineer failure (bad LLM response, validation, etc.)."""


class PlanNotSubmittedError(EngineerError):
    """Engineer attempted a non-plan action before submitting its plan."""


IssueStatus = Literal["pending", "in_progress", "done", "blocked", "skipped"]


class Issue(BaseModel):
    """One unit of work in the Engineer's plan.

    ``spec_refs`` lists capability ids from the EnrichedSpec this issue
    delivers. The QualityEngineer's review uses this to map tests back
    to spec capabilities.
    """
    id: str = PydField(..., description="Stable issue id, e.g. 'I1'.")
    title: str
    description: str
    target_files: List[str] = PydField(
        default_factory=list,
        description="Paths the Engineer expects to write/modify (workspace-relative).",
    )
    test_files: List[str] = PydField(
        default_factory=list,
        description="Paths of tests this issue ships (workspace-relative).",
    )
    success_criteria: List[str] = PydField(default_factory=list)
    spec_refs: List[str] = PydField(
        default_factory=list,
        description="EnrichedSpec capability ids this issue delivers.",
    )
    depends_on: List[str] = PydField(
        default_factory=list,
        description="Other issue ids that must complete first.",
    )
    status: IssueStatus = "pending"


class EngineerPlan(BaseModel):
    """The Engineer's submitted plan: ordered issues + narrative approach."""
    approach: str = PydField(
        ...,
        description="2-5 sentence summary of how the milestone will be implemented.",
    )
    issues: List[Issue] = PydField(default_factory=list)

    def get_issue(self, issue_id: str) -> Optional[Issue]:
        for i in self.issues:
            if i.id == issue_id:
                return i
        return None


class EngineerResult(BaseModel):
    """Terminal payload from ``submit_implementation``."""
    plan: EngineerPlan
    summary: str = ""
    final_test_status: Literal[
        "passed", "partial", "failed", "not_run"
    ] = "not_run"
    completed_issue_ids: List[str] = PydField(default_factory=list)
    deferred_issue_ids: List[str] = PydField(default_factory=list)
    notes: List[str] = PydField(default_factory=list)
