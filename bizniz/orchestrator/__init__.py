"""v2.5 Orchestrator — per-issue Coder dispatch with model escalation.

The Orchestrator owns the loop:
    for issue in topo_sorted_issues:
        coder = build_coder(progression.current_model)
        try: result = coder.code_issue(issue, ...)
        except ToolLoopAgentStalledError:
            if progression.escalate(): retry
            else: mark issue stalled, continue
"""
from bizniz.orchestrator.orchestrator import Orchestrator
from bizniz.orchestrator.types import IssueOutcome, OrchestratorResult

__all__ = ["Orchestrator", "OrchestratorResult", "IssueOutcome"]
