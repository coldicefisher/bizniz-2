"""
Tests for Autocoder multi-file generate and repair methods.
"""
import json
import pytest
from unittest.mock import MagicMock, call

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.autocoder.autocoder import Autocoder
from bizniz.autocoder.types import FileChange, AutocoderBadAIResponseError


def make_response(response_json):
    text = json.dumps(response_json)
    return text, "mock_job_id", [{"role": "assistant", "content": text}]


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


MULTI_GENERATE_RESPONSE = {
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
    ]
}

MULTI_REPAIR_RESPONSE = {
    "analysis": "Missing import in __init__.py",
    "fix_plan": "Add the import statement",
    "changes": [
        {
            "filepath": "pkg/__init__.py",
            "code": "from .models import Expense\nfrom .cli import main\n",
            "action": "modify",
        },
    ]
}


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

    def test_passes_architecture_context(self, autocoder, mock_client):
        mock_client.get_text.return_value = make_response(MULTI_GENERATE_RESPONSE)

        autocoder.generate_multi(
            issue_description="Create models",
            target_files=[{"filepath": "pkg/models.py", "action": "create"}],
            architecture_context="Package: expense_tracker\nNamespace: expense_tracker.models",
        )

        # Verify the prompt sent to AI includes the architecture context
        sent_messages = mock_client.get_text.call_args[1].get("messages") or mock_client.get_text.call_args[0][0]
        # The architecture context should appear in the user message that was added to history
        user_messages = [m for m in sent_messages if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) == "user"]
        assert any("expense_tracker" in (m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) for m in user_messages)

    def test_passes_existing_code(self, autocoder, mock_client):
        mock_client.get_text.return_value = make_response(MULTI_GENERATE_RESPONSE)

        autocoder.generate_multi(
            issue_description="Add CLI",
            target_files=[{"filepath": "pkg/cli.py", "action": "create"}],
            existing_code={"pkg/models.py": "class Expense:\n    pass\n"},
        )

        sent_messages = mock_client.get_text.call_args[1].get("messages") or mock_client.get_text.call_args[0][0]
        user_messages = [m for m in sent_messages if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) == "user"]
        assert any("class Expense" in (m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) for m in user_messages)

    def test_raises_on_empty_response(self, autocoder, mock_client):
        mock_client.get_text.return_value = ("", "jid", [{"role": "assistant", "content": ""}])

        with pytest.raises(AutocoderBadAIResponseError):
            autocoder.generate_multi(
                issue_description="Create models",
                target_files=[{"filepath": "pkg/models.py", "action": "create"}],
            )

    def test_raises_on_empty_changes(self, autocoder, mock_client):
        mock_client.get_text.return_value = make_response({"changes": []})

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
