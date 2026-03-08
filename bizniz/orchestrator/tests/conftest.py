import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bizniz.autocoder.autocoder import Autocoder
from bizniz.autotester.autotester import Autotester
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import ExecutionEnvironmentResult
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.autocoder.types import AutocoderProcessResult, FileChange
from bizniz.autotester.types import AutotesterResult, GeneratedTestFile
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator


GENERATED_CODE = "def add(a, b):\n    return a + b\n"
GENERATED_TESTS = "def test_add():\n    assert add(1, 2) == 3\n"


@pytest.fixture
def mock_autocoder():
    ac = MagicMock(spec=Autocoder)
    ac.generate_only.return_value = AutocoderProcessResult(changes=[FileChange(filepath="add.py", code=GENERATED_CODE, action="create")])
    ac.repair.return_value = AutocoderProcessResult(changes=[FileChange(filepath="add.py", code=GENERATED_CODE + "# repaired\n", action="modify")])
    return ac


@pytest.fixture
def mock_autotester():
    at = MagicMock(spec=Autotester)
    at.process_from_prompt.return_value = AutotesterResult(
        test_files=[GeneratedTestFile(filepath="test_add.py", tests=GENERATED_TESTS)],
        mode="from_prompt",
        success=True,
    )
    return at


@pytest.fixture
def mock_test_env():
    env = MagicMock(spec=BaseExecutionEnvironment)
    env.execute.return_value = ExecutionEnvironmentResult(success=True)
    return env


@pytest.fixture
def mock_workspace(tmp_path):
    ws = MagicMock(spec=BaseWorkspace)
    ws.path.return_value = tmp_path / "test_add.py"
    ws.read_file.return_value = GENERATED_CODE
    return ws


@pytest.fixture
def orchestrator(mock_autocoder, mock_autotester, mock_test_env, mock_workspace):
    return CodingOrchestrator(
        autocoder=mock_autocoder,
        autotester=mock_autotester,
        test_environment=mock_test_env,
        workspace=mock_workspace,
        max_iterations=5,
    )
