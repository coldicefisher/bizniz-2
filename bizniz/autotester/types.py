import datetime
from typing import Optional, List, Literal

from pydantic import BaseModel, Field


class GeneratedTestFile(BaseModel):
    """A single test file in a multi-file test suite."""
    filepath: str
    tests: str


class AutotesterResult(BaseModel):
    test_files: List[GeneratedTestFile] = []
    dependencies: List[str] = []
    mode: Literal["from_code", "from_prompt", "review"]
    success: bool
    error: Optional[str] = None


class AutotesterOnEventCallback(BaseModel):
    stage: Literal["generate", "save"]
    status: Literal["start", "success", "failure"]
    attempt: Optional[int] = None
    tests: Optional[str] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    timestamp: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class AutotesterError(Exception):
    pass


class AutotesterBadAIResponseError(AutotesterError):
    pass
