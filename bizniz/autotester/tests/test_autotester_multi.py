"""
Tests for Autotester multi-file test generation.
"""
import json
import pytest
from unittest.mock import MagicMock, call

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.autotester.autotester import Autotester
from bizniz.autotester.types import AutotesterBadAIResponseError


def make_response(response_json):
    text = json.dumps(response_json)
    return text, "mock_job_id", [{"role": "assistant", "content": text}]


# Tool-action envelope: the tool loop expects these fields
MULTI_TEST_RESPONSE = {
    "thinking": "Generating tests for the issue",
    "action": "submit_tests",
    "path": "",
    "test_files": [
        {
            "filepath": "tests/test_models.py",
            "tests": "import pytest\nfrom pkg.models import Expense\n\ndef test_expense_creation():\n    e = Expense()\n    assert e is not None\n",
        },
        {
            "filepath": "tests/test_cli.py",
            "tests": "import pytest\nfrom pkg.cli import main\n\ndef test_main_runs():\n    assert main() is None\n",
        },
    ],
    "notes": "Tests for models and CLI modules.",
    "dependencies": [],
}


@pytest.fixture
def mock_client():
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = make_response(MULTI_TEST_RESPONSE)
    return client


@pytest.fixture
def mock_environment():
    env = MagicMock(spec=BaseExecutionEnvironment)
    env.describe.return_value = "Test environment"
    return env


@pytest.fixture
def mock_workspace():
    ws = MagicMock(spec=BaseWorkspace)
    ws.root = "/tmp/test_workspace"
    return ws


@pytest.fixture
def autotester(mock_client, mock_environment, mock_workspace):
    return Autotester(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=3,
    )


class TestGenerateMulti:

    def test_returns_all_test_files(self, autotester):
        result = autotester.generate_multi(
            problem_statement="Build expense tracker",
            test_files=["tests/test_models.py", "tests/test_cli.py"],
        )

        assert result.success is True
        assert len(result.test_files) == 2
        assert result.test_files[0].filepath == "tests/test_models.py"
        assert "test_expense_creation" in result.test_files[0].tests
        assert result.test_files[1].filepath == "tests/test_cli.py"
        assert "test_main_runs" in result.test_files[1].tests

    def test_saves_all_test_files_to_workspace(self, autotester, mock_workspace):
        autotester.generate_multi(
            problem_statement="Build expense tracker",
            test_files=["tests/test_models.py", "tests/test_cli.py"],
        )

        assert mock_workspace.write_file.call_count == 2
        written_paths = [c[1].get("path") if len(c) > 1 else c[0][0] for c in [
            (mock_workspace.write_file.call_args_list[i].args, mock_workspace.write_file.call_args_list[i].kwargs)
            for i in range(2)
        ]]
        assert "tests/test_models.py" in written_paths
        assert "tests/test_cli.py" in written_paths

    def test_uses_tool_loop_with_discovery(self, autotester, mock_client, mock_workspace):
        """Verify the tool loop processes discovery tool calls before terminal action."""
        list_response = {
            "thinking": "Let me see the workspace",
            "action": "list_directory",
            "path": ".",
            "test_files": [],
            "notes": "",
            "dependencies": [],
        }
        mock_client.get_text.side_effect = [
            make_response(list_response),
            make_response(MULTI_TEST_RESPONSE),
        ]
        mock_workspace.list_relative_files.return_value = ["pkg/models.py"]

        result = autotester.generate_multi(
            problem_statement="Build tracker",
            test_files=["tests/test_models.py"],
        )

        assert len(result.test_files) >= 1
        assert mock_client.get_text.call_count == 2

    def test_raises_on_empty_response(self, autotester, mock_client):
        mock_client.get_text.return_value = ("", "jid", [{"role": "assistant", "content": ""}])

        with pytest.raises(AutotesterBadAIResponseError):
            autotester.generate_multi(
                problem_statement="Build tracker",
                test_files=["tests/test_models.py"],
            )

    def test_raises_on_empty_test_files(self, autotester, mock_client):
        empty_response = {
            "thinking": "Nothing to test",
            "action": "submit_tests",
            "path": "",
            "test_files": [],
            "notes": "",
            "dependencies": [],
        }
        mock_client.get_text.return_value = make_response(empty_response)

        with pytest.raises(AutotesterBadAIResponseError):
            autotester.generate_multi(
                problem_statement="Build tracker",
                test_files=["tests/test_models.py"],
            )

    def test_mode_is_from_prompt(self, autotester):
        result = autotester.generate_multi(
            problem_statement="Build tracker",
            test_files=["tests/test_models.py"],
        )
        assert result.mode == "from_prompt"


class TestGenerateTestsSingleFileFallback:
    """Verify that _generate_tests handles both old and new schema formats."""

    def test_handles_test_files_array(self, autotester, mock_client):
        response = {
            "test_files": [{"filepath": "tests/test_a.py", "tests": "def test_a(): pass\n"}],
            "notes": "ok",
        }
        mock_client.get_text.return_value = make_response(response)

        result = autotester.process_from_prompt(
            prompt="Test something",
            output_path="tests/test_a.py",
        )
        assert result.success is True
        assert "test_a" in result.test_files[0].tests

    def test_handles_old_tests_string(self, autotester, mock_client):
        response = {
            "tests": "def test_old(): pass\n",
            "notes": "ok",
        }
        mock_client.get_text.return_value = make_response(response)

        result = autotester.process_from_prompt(
            prompt="Test something",
            output_path="tests/test_old.py",
        )
        assert result.success is True
        assert "test_old" in result.test_files[0].tests
