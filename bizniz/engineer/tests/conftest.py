import json
import pytest
from unittest.mock import MagicMock

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.types import OrchestratorResult
from bizniz.engineer.auto_engineer import AutoEngineer


VALID_ANALYSIS_RESPONSE = {
    "business_requirements": ["Users can manage tasks."],
    "use_cases": [
        {"title": "Create Task", "description": "A user creates a new task."}
    ],
    "functional_requirements": ["The system stores tasks persistently."],
    "nonfunctional_requirements": ["Response time < 500 ms."],
    "issues": [
        {
            "title": "Implement task storage",
            "description": "Create a module to store and retrieve tasks.",
            "code_file": "task_storage.py",
            "test_file": "test_task_storage.py",
        }
    ],
}


def make_ai_response(data):
    text = json.dumps(data)
    return text, "job_id", [{"role": "assistant", "content": text}]


@pytest.fixture
def mock_client():
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = make_ai_response(VALID_ANALYSIS_RESPONSE)
    return client


@pytest.fixture
def mock_environment():
    env = MagicMock(spec=BaseExecutionEnvironment)
    env.describe.return_value = "Test env"
    return env


@pytest.fixture
def mock_workspace(tmp_path):
    return BaseWorkspace(root=tmp_path)


@pytest.fixture
def mock_orchestrator():
    orc = MagicMock(spec=CodingOrchestrator)
    orc.run.return_value = OrchestratorResult(success=True, code="pass", tests="pass", iterations=1)
    return orc


@pytest.fixture
def engineer(mock_client, mock_environment, mock_workspace, mock_orchestrator):
    return AutoEngineer(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        orchestrator_factory=lambda: mock_orchestrator,
        max_retries=3,
    )
