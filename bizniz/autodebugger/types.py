# Backward-compatibility shim — implementation moved to bizniz.agents.debugger.types
from bizniz.agents.debugger.types import (
    AutodebuggerDiagnosis,
    AutodebuggerOnEventCallback,
    AutodebuggerError,
    AutodebuggerBadAIResponseError,
)

__all__ = [
    "AutodebuggerDiagnosis",
    "AutodebuggerOnEventCallback",
    "AutodebuggerError",
    "AutodebuggerBadAIResponseError",
]
