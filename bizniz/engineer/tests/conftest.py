import json
import pytest
from unittest.mock import MagicMock

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.autocoder.types import FileChange
from bizniz.autotester.types import GeneratedTestFile
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
            "target_files": [{"filepath": "task_manager/storage.py", "action": "create"}],
            "test_files": ["tests/test_storage.py"],
            "depends_on": [],
        }
    ],
}

VALID_PLAN_RESPONSE = {
    "package_name": "task_manager",
    "root_namespace": "task_manager",
    "namespaces": [
        {"namespace_path": "task_manager", "purpose": "Root package"},
    ],
    "domain_models": [],
    "modules": [
        {
            "filepath": "task_manager/storage.py",
            "class_name": "TaskStorage",
            "namespace_path": "task_manager",
            "methods": [
                {"name": "save", "signature": "def save(self, task: dict) -> int", "description": "Save a task"},
            ],
            "docstring": "Stores and retrieves tasks.",
        }
    ],
    "dependencies": [],
}


def make_ai_response(data):
    text = json.dumps(data)
    return text, "job_id", [{"role": "assistant", "content": text}]


def _make_multi_response_client():
    """Create a mock client that returns analysis, then plan, then refined analysis."""
    client = MagicMock(spec=BaseAIClient)
    client.get_text.side_effect = [
        make_ai_response(VALID_ANALYSIS_RESPONSE),  # initial analysis
        make_ai_response(VALID_PLAN_RESPONSE),       # architecture plan
        make_ai_response(VALID_ANALYSIS_RESPONSE),   # refined analysis
    ]
    return client


@pytest.fixture
def mock_client():
    return _make_multi_response_client()


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
    orc.run_multi.return_value = OrchestratorResult(success=True, changes=[FileChange(filepath="task_manager/storage.py", code="pass", action="create")], test_files=[GeneratedTestFile(filepath="tests/test_storage.py", tests="pass")], iterations=1)
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
