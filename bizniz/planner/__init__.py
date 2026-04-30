"""
Planner — sequences user value into deliverable milestones.

The Planner sits above the Architect. Given a problem statement (and
optionally the existing project state for re-plans), it produces an
ordered list of milestones, each a self-contained problem-slice the
Architect can decompose later.

The Planner does NOT decide what services exist or what frameworks to
use — those are the Architect's concerns. The Planner reasons in terms
of user value: use cases, success criteria, and dependencies between
deliverable chunks.

Public API::

    from bizniz.planner import Planner, ProjectPlan, Milestone

    planner = Planner(client=top_tier_client, environment=env, workspace=ws)
    plan = planner.plan(problem_statement)
    for m in plan.milestones:
        print(m.sequence_index, m.name, m.use_cases)
"""
from bizniz.planner.planner import Planner
from bizniz.planner.types import (
    Milestone,
    ProjectPlan,
    PlannerError,
    PlannerBadAIResponseError,
)

__all__ = [
    "Planner",
    "Milestone",
    "ProjectPlan",
    "PlannerError",
    "PlannerBadAIResponseError",
]
