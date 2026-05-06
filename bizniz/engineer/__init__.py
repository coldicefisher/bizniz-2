"""Engineer — v2 milestone-scoped tool-loop agent.

ONE Engineer per milestone (not per service). Holds the full
EnrichedSpec, architecture, and auth contract; iterates with discovery
tools, writes code, runs tests, debugs. Emits a plan first, then an
implementation when satisfied.

Single concrete subclass of ``ToolLoopAgent``. The ``submit_plan``
action is mandatory as the first action — the loop rejects other
actions until the plan is on the record.
"""
from bizniz.engineer.agent import Engineer
from bizniz.engineer.types import (
    EngineerError,
    EngineerPlan,
    EngineerResult,
    Issue,
    PlanNotSubmittedError,
)

__all__ = [
    "Engineer",
    "EngineerError",
    "EngineerPlan",
    "EngineerResult",
    "Issue",
    "PlanNotSubmittedError",
]
