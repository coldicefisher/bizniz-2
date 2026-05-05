import pytest
import json
from unittest.mock import MagicMock

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import (
    ExecutionEnvironmentResult,
    ExecutionEnvironmentErrorDetails,
    ExecutionCallSpec,
)
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.agents.coder.coder import Coder


# ---------------------------------------------------------------------------
# Shared response helpers
# ---------------------------------------------------------------------------

VALID_GENERATE_JSON = {
    "code": "def add(a, b): return a + b",
    "call_spec": {"symbol": "add", "args": [1, 2], "kwargs": {}},
}

VALID_REPAIR_JSON = {
    "code": "def add(a, b): return a + b",
    "analysis": "Function was missing",
    "fix_plan": "Added the function",
    "call_spec": {"symbol": "add", "args": [1, 2], "kwargs": {}},
}


def make_get_text_response(response_json):
    """Return the 3-tuple that BaseChatGPTClient.get_text produces."""
    text = json.dumps(response_json)
    output_messages = [{"role": "assistant", "content": text}]
    return text, "mock_job_id", output_messages


# ---------------------------------------------------------------------------
# Core mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client():
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = make_get_text_response(VALID_GENERATE_JSON)
    return client


@pytest.fixture
def mock_environment():
    env = MagicMock(spec=BaseExecutionEnvironment)
    env.describe.return_value = "Test execution environment"
    env.execute.return_value = ExecutionEnvironmentResult(success=True, result=42)
    return env


@pytest.fixture
def mock_workspace(tmp_path):
    ws = MagicMock(spec=BaseWorkspace)
    ws.exists.return_value = False
    ws.path.return_value = tmp_path / "generated_code.py"
    return ws


@pytest.fixture
def coder(mock_client, mock_environment, mock_workspace):
    return Coder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=3,
    )
