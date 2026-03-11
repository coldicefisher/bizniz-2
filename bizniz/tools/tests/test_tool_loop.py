"""Tests for the shared tool-use conversation loop."""
import json
import pytest
from unittest.mock import MagicMock

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.tools.tool_loop import (
    run_tool_loop,
    ToolLoopError,
    ToolLoopTimeoutError,
    ToolLoopBadResponseError,
)
from bizniz.tools.schemas import build_tool_action_schema


def _make_schema():
    return build_tool_action_schema(
        name="test_action",
        terminal_action="submit",
        terminal_properties={
            "result": {"type": "string"},
        },
        terminal_required=["result"],
    )


def _make_response(action_dict):
    text = json.dumps(action_dict)
    return text, "job_id", [{"role": "assistant", "content": text}]


class TestToolLoopTerminalAction:

    def test_returns_immediately_on_terminal_action(self):
        client = MagicMock(spec=BaseAIClient)
        workspace = MagicMock(spec=BaseWorkspace)

        terminal = {
            "thinking": "done",
            "action": "submit",
            "path": "",
            "result": "hello",
        }
        client.get_text.return_value = _make_response(terminal)

        result = run_tool_loop(
            client=client,
            workspace=workspace,
            system_prompt="You are a test agent.",
            initial_user_message="Do something.",
            action_schema=_make_schema(),
            terminal_action="submit",
            max_turns=5,
        )

        assert result["action"] == "submit"
        assert result["result"] == "hello"
        assert client.get_text.call_count == 1


class TestToolLoopDiscovery:

    def test_processes_view_file_then_submits(self):
        client = MagicMock(spec=BaseAIClient)
        workspace = MagicMock(spec=BaseWorkspace)
        workspace.read_file.return_value = "file content here"

        view_action = {
            "thinking": "let me read the file",
            "action": "view_file",
            "path": "main.py",
            "result": "",
        }
        terminal = {
            "thinking": "got it",
            "action": "submit",
            "path": "",
            "result": "done",
        }
        client.get_text.side_effect = [
            _make_response(view_action),
            _make_response(terminal),
        ]

        result = run_tool_loop(
            client=client,
            workspace=workspace,
            system_prompt="Test",
            initial_user_message="Go",
            action_schema=_make_schema(),
            terminal_action="submit",
        )

        assert result["result"] == "done"
        assert client.get_text.call_count == 2
        workspace.read_file.assert_called_once_with(path="main.py")

    def test_processes_list_directory(self):
        client = MagicMock(spec=BaseAIClient)
        workspace = MagicMock(spec=BaseWorkspace)
        workspace.tree.return_value = ["a.py", "b.py"]

        list_action = {
            "thinking": "check structure",
            "action": "list_directory",
            "path": ".",
            "result": "",
        }
        terminal = {
            "thinking": "done",
            "action": "submit",
            "path": "",
            "result": "ok",
        }
        client.get_text.side_effect = [
            _make_response(list_action),
            _make_response(terminal),
        ]

        result = run_tool_loop(
            client=client,
            workspace=workspace,
            system_prompt="Test",
            initial_user_message="Go",
            action_schema=_make_schema(),
            terminal_action="submit",
        )

        assert result["result"] == "ok"

    def test_processes_search_files(self, tmp_path):
        client = MagicMock(spec=BaseAIClient)
        workspace = MagicMock(spec=BaseWorkspace)
        workspace.root = tmp_path

        search_action = {
            "thinking": "find class",
            "action": "search_files",
            "path": "class Foo",
            "result": "",
        }
        terminal = {
            "thinking": "done",
            "action": "submit",
            "path": "",
            "result": "found",
        }
        client.get_text.side_effect = [
            _make_response(search_action),
            _make_response(terminal),
        ]

        result = run_tool_loop(
            client=client,
            workspace=workspace,
            system_prompt="Test",
            initial_user_message="Go",
            action_schema=_make_schema(),
            terminal_action="submit",
        )

        assert result["result"] == "found"


class TestToolLoopErrors:

    def test_raises_on_empty_response(self):
        client = MagicMock(spec=BaseAIClient)
        workspace = MagicMock(spec=BaseWorkspace)
        client.get_text.return_value = ("", "jid", [])

        with pytest.raises(ToolLoopBadResponseError):
            run_tool_loop(
                client=client,
                workspace=workspace,
                system_prompt="Test",
                initial_user_message="Go",
                action_schema=_make_schema(),
                terminal_action="submit",
                max_turns=5,
            )

    def test_raises_on_max_turns_exhausted(self):
        client = MagicMock(spec=BaseAIClient)
        workspace = MagicMock(spec=BaseWorkspace)
        workspace.tree.return_value = ["a.py"]

        # Always returns a non-terminal action
        non_terminal = {
            "thinking": "exploring",
            "action": "list_directory",
            "path": ".",
            "result": "",
        }
        client.get_text.return_value = _make_response(non_terminal)

        with pytest.raises(ToolLoopTimeoutError):
            run_tool_loop(
                client=client,
                workspace=workspace,
                system_prompt="Test",
                initial_user_message="Go",
                action_schema=_make_schema(),
                terminal_action="submit",
                max_turns=2,
            )


class TestToolLoopExtraHandlers:

    def test_dispatches_to_extra_handler(self):
        client = MagicMock(spec=BaseAIClient)
        workspace = MagicMock(spec=BaseWorkspace)

        custom_action = {
            "thinking": "running custom",
            "action": "run_tests",
            "path": "tests/",
            "result": "",
        }
        terminal = {
            "thinking": "done",
            "action": "submit",
            "path": "",
            "result": "passed",
        }
        client.get_text.side_effect = [
            _make_response(custom_action),
            _make_response(terminal),
        ]

        handler = MagicMock(return_value="TESTS PASSED")

        # Need a schema that includes run_tests
        schema = build_tool_action_schema(
            name="test_with_extra",
            terminal_action="submit",
            terminal_properties={"result": {"type": "string"}},
            terminal_required=["result"],
            extra_actions=["run_tests"],
        )

        result = run_tool_loop(
            client=client,
            workspace=workspace,
            system_prompt="Test",
            initial_user_message="Go",
            action_schema=schema,
            terminal_action="submit",
            extra_tool_handlers={"run_tests": handler},
        )

        assert result["result"] == "passed"
        handler.assert_called_once()


class TestBuildToolActionSchema:

    def test_builds_valid_schema(self):
        schema = build_tool_action_schema(
            name="test_schema",
            terminal_action="submit_code",
            terminal_properties={
                "changes": {"type": "array", "items": {"type": "string"}},
            },
            terminal_required=["changes"],
        )

        assert schema["name"] == "test_schema"
        assert schema["strict"] is True

        props = schema["schema"]["properties"]
        assert "thinking" in props
        assert "action" in props
        assert "path" in props
        assert "changes" in props

        actions = props["action"]["enum"]
        assert "view_file" in actions
        assert "list_directory" in actions
        assert "search_files" in actions
        assert "submit_code" in actions

    def test_includes_extra_actions(self):
        schema = build_tool_action_schema(
            name="test",
            terminal_action="submit",
            terminal_properties={"result": {"type": "string"}},
            terminal_required=["result"],
            extra_actions=["run_command", "run_tests"],
        )

        actions = schema["schema"]["properties"]["action"]["enum"]
        assert "run_command" in actions
        assert "run_tests" in actions
        assert "submit" in actions
