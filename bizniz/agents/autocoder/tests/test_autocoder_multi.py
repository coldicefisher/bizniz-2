"""
Tests for Autocoder multi-file generate and repair methods.
"""
import json
import pytest
from unittest.mock import MagicMock, call

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.agents.autocoder.autocoder import Autocoder
from bizniz.agents.autocoder.types import FileChange, AutocoderBadAIResponseError


def make_response(response_json):
    text = json.dumps(response_json)
    return text, "mock_job_id", [{"role": "assistant", "content": text}]


# Tool-action envelope: the tool loop expects these fields
MULTI_GENERATE_RESPONSE = {
    "thinking": "Generating code for the issue",
    "action": "submit_code",
    "path": "",
    "changes": [
        {
            "filepath": "pkg/models.py",
            "code": "class Expense:\n    pass\n",
            "action": "create",
        },
        {
            "filepath": "pkg/__init__.py",
            "code": "from .models import Expense\n",
            "action": "modify",
        },
    ],
    "dependencies": [],
}

MULTI_REPAIR_RESPONSE = {
    "thinking": "Fixing the import error",
    "action": "submit_code",
    "path": "",
    "analysis": "Missing import in __init__.py",
    "fix_plan": "Add the import statement",
    "changes": [
        {
            "filepath": "pkg/__init__.py",
            "code": "from .models import Expense\nfrom .cli import main\n",
            "action": "modify",
        },
    ],
    "dependencies": [],
}


@pytest.fixture
def mock_client():
    return MagicMock(spec=BaseAIClient)


@pytest.fixture
def mock_environment():
    env = MagicMock(spec=BaseExecutionEnvironment)
    env.describe.return_value = "Test environment"
    return env


@pytest.fixture
def mock_workspace():
    ws = MagicMock(spec=BaseWorkspace)
    ws.exists.return_value = False
    return ws


@pytest.fixture
def autocoder(mock_client, mock_environment, mock_workspace):
    return Autocoder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=3,
    )


class TestGenerateMulti:

    def test_returns_all_file_changes(self, autocoder, mock_client):
        mock_client.get_text.return_value = make_response(MULTI_GENERATE_RESPONSE)

        result = autocoder.generate_multi(
            issue_description="Create expense models",
            target_files=[
                {"filepath": "pkg/models.py", "action": "create"},
                {"filepath": "pkg/__init__.py", "action": "modify"},
            ],
        )

        assert len(result.changes) == 2
        assert result.changes[0].filepath == "pkg/models.py"
        assert result.changes[0].action == "create"
        assert "class Expense" in result.changes[0].code
        assert result.changes[1].filepath == "pkg/__init__.py"
        assert result.changes[1].action == "modify"

    def test_saves_all_files_to_workspace(self, autocoder, mock_client, mock_workspace):
        mock_client.get_text.return_value = make_response(MULTI_GENERATE_RESPONSE)

        autocoder.generate_multi(
            issue_description="Create models",
            target_files=[
                {"filepath": "pkg/models.py", "action": "create"},
                {"filepath": "pkg/__init__.py", "action": "modify"},
            ],
        )

        assert mock_workspace.write_file.call_count == 2
        calls = mock_workspace.write_file.call_args_list
        assert calls[0][1]["path"] == "pkg/models.py"
        assert "class Expense" in calls[0][1]["content"]
        assert calls[1][1]["path"] == "pkg/__init__.py"
        assert "from .models import Expense" in calls[1][1]["content"]

    def test_uses_tool_loop_with_discovery(self, autocoder, mock_client, mock_workspace):
        """Verify the tool loop processes discovery tool calls before terminal action."""
        # First call: LLM asks to list directory
        list_response = {
            "thinking": "Let me see the workspace",
            "action": "list_directory",
            "path": ".",
            "changes": [],
            "dependencies": [],
        }
        # Second call: LLM submits code
        mock_client.get_text.side_effect = [
            make_response(list_response),
            make_response(MULTI_GENERATE_RESPONSE),
        ]
        mock_workspace.list_relative_files.return_value = ["pkg/models.py"]

        result = autocoder.generate_multi(
            issue_description="Create models",
            target_files=[{"filepath": "pkg/models.py", "action": "create"}],
        )

        assert len(result.changes) == 2
        assert mock_client.get_text.call_count == 2

    def test_raises_on_empty_response(self, autocoder, mock_client):
        mock_client.get_text.return_value = ("", "jid", [{"role": "assistant", "content": ""}])

        with pytest.raises(AutocoderBadAIResponseError):
            autocoder.generate_multi(
                issue_description="Create models",
                target_files=[{"filepath": "pkg/models.py", "action": "create"}],
            )

    def test_raises_on_empty_changes(self, autocoder, mock_client):
        empty_response = {
            "thinking": "Nothing to do",
            "action": "submit_code",
            "path": "",
            "changes": [],
            "dependencies": [],
        }
        mock_client.get_text.return_value = make_response(empty_response)

        with pytest.raises(AutocoderBadAIResponseError):
            autocoder.generate_multi(
                issue_description="Create models",
                target_files=[{"filepath": "pkg/models.py", "action": "create"}],
            )


class TestRepairMulti:

    def test_returns_repaired_changes(self, autocoder, mock_client):
        mock_client.get_text.return_value = make_response(MULTI_REPAIR_RESPONSE)

        result = autocoder.repair_multi(
            current_files={"pkg/__init__.py": "# empty\n"},
            error_message="ImportError: cannot import 'main'",
        )

        assert len(result.changes) == 1
        assert result.changes[0].filepath == "pkg/__init__.py"
        assert "from .cli import main" in result.changes[0].code

    def test_saves_repaired_files_to_workspace(self, autocoder, mock_client, mock_workspace):
        mock_client.get_text.return_value = make_response(MULTI_REPAIR_RESPONSE)

        autocoder.repair_multi(
            current_files={"pkg/__init__.py": "# empty\n"},
            error_message="ImportError",
        )

        mock_workspace.write_file.assert_called_once()
        written = mock_workspace.write_file.call_args[1]
        assert written["path"] == "pkg/__init__.py"
        assert "from .models import Expense" in written["content"]
        assert "from .cli import main" in written["content"]

    def test_raises_on_repeated_failure(self, autocoder, mock_client):
        mock_client.get_text.return_value = ("", "jid", [{"role": "assistant", "content": ""}])

        with pytest.raises(AutocoderBadAIResponseError):
            autocoder.repair_multi(
                current_files={"pkg/__init__.py": "# empty\n"},
                error_message="ImportError",
            )


class TestExtractCodeFromResponse:

    def test_extracts_from_changes_array(self):
        resp = {"changes": [{"filepath": "a.py", "code": "print('hi')", "action": "create"}]}
        assert Autocoder._extract_code_from_response(resp) == "print('hi')"

    def test_falls_back_to_code_field(self):
        resp = {"code": "print('hi')"}
        assert Autocoder._extract_code_from_response(resp) == "print('hi')"

    def test_prefers_changes_over_code(self):
        resp = {"code": "old", "changes": [{"code": "new"}]}
        assert Autocoder._extract_code_from_response(resp) == "new"

    def test_empty_changes_falls_back(self):
        resp = {"code": "fallback", "changes": []}
        assert Autocoder._extract_code_from_response(resp) == "fallback"
