# bizniz.agents.debugger — unified debugger module
#
# Two implementations:
#   QuickDebugger  — one-shot diagnosis (no tools, fast, cheap)
#   AgenticDebugger — iterative tool-use diagnosis (powerful, expensive)

from bizniz.agents.debugger.quick import QuickDebugger
from bizniz.agents.debugger.agentic import AgenticDebugger
from bizniz.agents.debugger.base import BaseDebugger

# Backward-compatible alias
QuickDebugger = QuickDebugger

__all__ = [
    "BaseDebugger",
    "QuickDebugger",
    "AgenticDebugger",
    "QuickDebugger",
]
