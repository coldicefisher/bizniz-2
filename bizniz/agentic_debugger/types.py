# Backward-compatibility shim — implementation moved to bizniz.agents.debugger.types
from bizniz.agents.debugger.types import (
    AgenticDiagnosis,
    CodeFix,
    AgenticDebuggerError,
    AgenticDebuggerTimeoutError,
    AgenticDebuggerGaveUpError,
    AgenticDebuggerBadResponseError,
)

__all__ = [
    "AgenticDiagnosis",
    "CodeFix",
    "AgenticDebuggerError",
    "AgenticDebuggerTimeoutError",
    "AgenticDebuggerGaveUpError",
    "AgenticDebuggerBadResponseError",
]
