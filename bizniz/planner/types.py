"""Planner result types."""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field

from bizniz.auth.spec import AuthSpecDelta


class Milestone(BaseModel):
    """A single deliverable chunk the project ships incrementally.

    The Planner outputs an ordered list of these. Each milestone is a
    self-contained slice of user value — the Architect can take its
    ``problem_slice`` as a standalone problem statement and decompose
    it into services / issues just like a greenfield project.

    The ``auth_delta`` field is the typed contract for auth changes in
    this milestone. The Architect accumulates deltas across milestones
    to produce the cumulative AuthSpec the provisioner materializes.
    See ``bizniz/auth/spec.py``.
    """
    db_id: Optional[int] = None
    sequence_index: int = 0  # 0-based position in the plan
    name: str
    problem_slice: str       # self-contained problem statement just for this milestone
    use_cases: List[str] = []         # user stories shipped in this milestone
    success_criteria: List[str] = []  # testable outcomes
    depends_on_names: List[str] = []  # other milestone names that must ship first
    estimated_effort: Optional[str] = None  # rough sizing (human review hint)
    status: str = "planned"   # planned | in_progress | completed | skipped
    auth_delta: AuthSpecDelta = Field(default_factory=AuthSpecDelta)


class ProjectPlan(BaseModel):
    """The Planner's output: an ordered sequence of milestones."""
    db_id: Optional[int] = None
    project_slug: str
    problem_statement: str
    description: str = ""
    milestones: List[Milestone] = []


class PlannerError(Exception):
    pass


class PlannerBadAIResponseError(PlannerError):
    """The AI's response could not be parsed into a valid ProjectPlan."""
    pass
