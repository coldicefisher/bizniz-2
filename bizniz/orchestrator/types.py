from typing import Optional, List
from pydantic import BaseModel

from bizniz.autocoder.types import FileChange
from bizniz.autotester.types import GeneratedTestFile


class TestRunResult(BaseModel):
    """Result of running ALL project tests."""
    all_passed: bool
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    failing_test_files: List[str] = []
    regression_files: List[str] = []  # tests that were passing before but now fail
    stdout: str = ""


class OrchestratorResult(BaseModel):
    success: bool
    changes: List[FileChange] = []
    test_files: List[GeneratedTestFile] = []
    iterations: int = 0
    error: Optional[str] = None
    failure_context: Optional[str] = None  # last failure output for retry strategies
    strategy_used: Optional[str] = None  # "tdd" or "code_first"
    architecture_drift_detected: bool = False
    drift_files: List[str] = []  # unplanned filepaths changed by the autocoder


class OrchestratorStalledError(Exception):
    """Raised when the orchestrator detects the same code being produced twice in a row."""
    pass


class OrchestratorMaxIterationsError(Exception):
    """Raised when max_iterations is exhausted without a passing test run."""
    pass
