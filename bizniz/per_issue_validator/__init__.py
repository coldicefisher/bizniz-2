"""``per_issue_validator`` — v4 per-issue validation pipeline.

After a ``CoderTesterAgent`` finishes one issue, this module writes
the files to disk, runs deterministic scanners (symbol_validator +
AST + optional pytest collection), and if any defects are found,
loops back to the agent with a fix prompt until clean or stall.

The deterministic gates are non-negotiable — they're spec-blind and
can't be fooled by an agent that misread the spec. The "agentic
debug" pass is the same CoderTesterAgent re-invoked with findings
as additional context.
"""
from bizniz.per_issue_validator.debugger import (
    PerIssueDebugger,
    PerIssueDebuggerError,
)
from bizniz.per_issue_validator.types import (
    Finding,
    ValidatedIssue,
)
from bizniz.per_issue_validator.validator import PerIssueValidator

__all__ = [
    "Finding",
    "ValidatedIssue",
    "PerIssueValidator",
    "PerIssueDebugger",
    "PerIssueDebuggerError",
]
