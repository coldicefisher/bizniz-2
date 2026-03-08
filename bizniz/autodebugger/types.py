import datetime
from typing import Optional, List, Literal, Dict

from pydantic import BaseModel, Field


class AutodebuggerDiagnosis(BaseModel):
    """Structured diagnosis produced by the Autodebugger agent."""
    diagnosis: str
    fix_target: Literal["code", "tests"]
    relevant_files: Dict[str, str] = Field(default_factory=dict)
    suggested_approach: str
    affected_files: List[str] = []  # which files should be modified to fix the issue


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


class AutodebuggerError(Exception):
    pass


class AutodebuggerBadAIResponseError(AutodebuggerError):
    pass
