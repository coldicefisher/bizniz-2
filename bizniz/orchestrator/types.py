from typing import Optional, Any
from pydantic import BaseModel


class OrchestratorResult(BaseModel):
    success: bool
    code: Optional[str] = None
    tests: Optional[str] = None
    iterations: int = 0
    error: Optional[str] = None


class OrchestratorStalledError(Exception):
    """Raised when the orchestrator detects the same code being produced twice in a row."""
    pass


class OrchestratorMaxIterationsError(Exception):
    """Raised when max_iterations is exhausted without a passing test run."""
    pass
