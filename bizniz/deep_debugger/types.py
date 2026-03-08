import datetime
from typing import Optional, List, Literal

from pydantic import BaseModel, Field


class DeepDiagnosis(BaseModel):
    """Comprehensive diagnosis produced when repairs have stalled."""
    root_cause: str
    root_cause_category: Literal[
        "logic_error", "interface_mismatch", "missing_implementation",
        "dependency_issue", "architectural_flaw", "test_issue"
    ]
    fix_target: Literal["code", "tests", "both"]
    affected_files: List[str]
    fix_plan: List[str]
    suggested_approach: str
    missing_packages: List[str] = []
    confidence: Literal["high", "medium", "low"]
    repair_history_analysis: str


class DeepDebuggerError(Exception):
    pass


class DeepDebuggerBadAIResponseError(DeepDebuggerError):
    pass
