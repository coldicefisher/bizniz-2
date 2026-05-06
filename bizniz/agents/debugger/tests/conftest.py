import pytest
from unittest.mock import MagicMock

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace


@pytest.fixture
def mock_client():
    return MagicMock(spec=BaseAIClient)


@pytest.fixture
def mock_environment():
    return MagicMock(spec=BaseExecutionEnvironment)


@pytest.fixture
def mock_workspace(tmp_path):
    ws = MagicMock(spec=BaseWorkspace)
    ws.list_relative_files.return_value = [
        "add_expense.py",
        "test_add_expense.py",
        "list_expenses.py",
        "test_list_expenses.py",
    ]
    ws.read_file.return_value = "# file contents\n"
    return ws
