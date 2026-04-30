import datetime
from typing import Optional, List, Literal

from pydantic import BaseModel, Field


class GeneratedTestFile(BaseModel):
    """A single test file in a multi-file test suite."""
    filepath: str
    tests: str


class TesterResult(BaseModel):
    # Tell pytest not to try to collect this as a test class — its name
    # starts with "Test" which matches pytest's default discovery rule.
    __test__ = False

    test_files: List[GeneratedTestFile] = []
    dependencies: List[str] = []
    mode: Literal["from_code", "from_prompt", "review"]
    success: bool
    error: Optional[str] = None


class TesterOnEventCallback(BaseModel):
    __test__ = False

    stage: Literal["generate", "save"]
    status: Literal["start", "success", "failure"]
    attempt: Optional[int] = None
    tests: Optional[str] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    timestamp: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class TesterError(Exception):
    __test__ = False


class TesterBadAIResponseError(TesterError):
    __test__ = False
