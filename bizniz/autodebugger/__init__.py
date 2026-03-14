# Backward-compatibility shim — implementation moved to bizniz.agents.debugger
from bizniz.agents.debugger.quick import QuickDebugger as Autodebugger
from bizniz.agents.debugger.types import AutodebuggerDiagnosis, AutodebuggerError, AutodebuggerBadAIResponseError

__all__ = ["Autodebugger", "AutodebuggerDiagnosis", "AutodebuggerError", "AutodebuggerBadAIResponseError"]
