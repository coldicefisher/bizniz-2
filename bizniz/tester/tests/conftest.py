import json
import pytest
from unittest.mock import MagicMock

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.tester.tester import Tester


VALID_TESTS_CODE = "def test_add():\n    assert 1 + 1 == 2\n"

VALID_AI_RESPONSE = {
    "tests": VALID_TESTS_CODE,
    "notes": "Basic test for addition.",
}


def make_get_text_response(response_json):
    text = json.dumps(response_json)
    output_messages = [{"role": "assistant", "content": text}]
    return text, "mock_job_id", output_messages


@pytest.fixture
def mock_client():
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = make_get_text_response(VALID_AI_RESPONSE)
    return client


@pytest.fixture
def mock_environment():
    env = MagicMock(spec=BaseExecutionEnvironment)
    env.describe.return_value = "Test execution environment"
    return env


@pytest.fixture
def mock_workspace(tmp_path):
    ws = MagicMock(spec=BaseWorkspace)
    ws.root = tmp_path
    ws.read_file.return_value = "def add(a, b): return a + b"
    ws.write_file.return_value = tmp_path / "test_output.py"
    return ws


@pytest.fixture
def tester(mock_client, mock_environment, mock_workspace):
    return Tester(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=3,
    )
