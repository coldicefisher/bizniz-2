"""``per_milestone_debugger`` — one smart tool-loop debugger that
sees the whole milestone, not one issue at a time.

User vision (2026-05-19): "Let the debugger have full capability.
We have to accept that it's slower. We need the coders to just
frame quickly."

PerMilestoneDebugger:
  - scope: whole milestone (all services, all files)
  - tools: Edit, Write, Read, Bash, Glob, Grep
  - context truncated when long (file slicing + finding cap)
  - sequential by design (one big agent session, not N parallel)
  - replaces PerIssueDebugger's escalation path in v4/v5

Slots into the v5 review/repair loop as the recovery mechanism
when canonical findings can't be resolved by structured fixes
alone (specifically: when the loop is about to stall or regress
and a smarter, whole-milestone agent might rescue it).
"""
from bizniz.per_milestone_debugger.debugger import (
    PerMilestoneDebugger,
    PerMilestoneDebuggerError,
    PerMilestoneDebuggerResult,
)

__all__ = [
    "PerMilestoneDebugger",
    "PerMilestoneDebuggerError",
    "PerMilestoneDebuggerResult",
]
