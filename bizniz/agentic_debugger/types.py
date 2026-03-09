"""
Types for the AgenticDebugger.
"""

from typing import Optional, List, Dict, Literal
from pydantic import BaseModel


class CodeFix(BaseModel):
    """A direct code fix produced by the debugger."""
    filepath: str
    new_content: str


class AgenticDiagnosis(BaseModel):
    """
    Unified diagnosis result from the AgenticDebugger.

    Combines fields from both the old AutodebuggerDiagnosis and DeepDiagnosis,
    plus optional direct code fixes.
    """
    diagnosis: str = ""
    root_cause_category: str = ""
    fix_target: Literal["code", "tests", "both"] = "code"
    affected_files: List[str] = []
    fix_plan: List[str] = []
    suggested_approach: str = ""
    missing_packages: List[str] = []
    confidence: Literal["high", "medium", "low"] = "medium"
    code_fixes: List[CodeFix] = []


class AgenticDebuggerError(Exception):
    """Base error for AgenticDebugger."""
    pass


class AgenticDebuggerTimeoutError(AgenticDebuggerError):
    """Raised when the debugger exceeds its time limit."""
    pass


class AgenticDebuggerGaveUpError(AgenticDebuggerError):
    """Raised when the debugger explicitly gives up."""
    pass


class AgenticDebuggerBadResponseError(AgenticDebuggerError):
    """Raised when the AI returns unparseable responses repeatedly."""
    pass
