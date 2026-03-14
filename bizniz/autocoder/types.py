# autocoder/types.py

import datetime

from typing import Optional, Callable, Union, Any, Dict, List, Tuple, Literal

from pydantic import BaseModel, Field


class AutocoderProcessError(Exception):
    pass


class AutocoderBadAIResponseError(AutocoderProcessError):
    pass


# FileChange lives in bizniz.core.types — re-exported here for backward compatibility
from bizniz.core.types import FileChange


class AutocoderProcessResult(BaseModel):
    changes: List[FileChange] = []
    dependencies: List[str] = []
    test_scaffold: str = ""
    output: Optional[Any] = None


class AutocoderAIVerificationResult(BaseModel):
    is_valid: bool
    code: Optional[str] = None
    errors: Optional[List[str]] = None


class AutocoderFailedError(BaseModel):
    error: str
    code: str
    failed_at: Literal["evaluation", "validation", "ai_verification"]
    recommended_code_changes: Optional[str] = None


class AutocoderOnEventCallback(BaseModel):
    stage: Literal["generate", "evaluation", "validation", "ai_verification", "repair", "process"]
    status: Literal["start", "success", "failure"]
    attempt: Optional[int] = None
    code: Optional[str] = None
    error: Optional[str] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    input_data: Optional[str] = None
    timestamp: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))


