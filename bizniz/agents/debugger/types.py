"""
Merged types for both QuickDebugger and AgenticDebugger.
"""

import datetime
from typing import Optional, List, Dict, Literal

from pydantic import BaseModel, Field


# --- Base error hierarchy ---

class DebuggerError(Exception):
    """Base error for all debugger agents."""
    pass


# --- Quick debugger types (was autodebugger) ---

class AutodebuggerError(DebuggerError):
    pass


class AutodebuggerBadAIResponseError(AutodebuggerError):
    pass


class AutodebuggerDiagnosis(BaseModel):
    """Structured diagnosis produced by the QuickDebugger agent."""
    diagnosis: str
    fix_target: Literal["code", "tests"]
    relevant_files: Dict[str, str] = Field(default_factory=dict)
    suggested_approach: str
    affected_files: List[str] = []


class AutodebuggerOnEventCallback(BaseModel):
    stage: Literal["scan", "diagnose"]
    status: Literal["start", "success", "failure"]
    attempt: Optional[int] = None
    diagnosis: Optional[str] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    timestamp: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


# --- Agentic debugger types ---

class AgenticDebuggerError(DebuggerError):
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
