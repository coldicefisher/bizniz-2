import json
import pytest
from unittest.mock import MagicMock

from bizniz.autotester.types import AutotesterResult, AutotesterBadAIResponseError


PROBLEM = "Build a function that checks if a string is a palindrome."


def test_from_prompt_returns_result(autotester, mock_workspace):
    result = autotester.process_from_prompt(prompt=PROBLEM, output_path="test_palindrome.py")

    assert isinstance(result, AutotesterResult)
    assert result.success is True
    assert result.mode == "from_prompt"
    assert result.output_path == "test_palindrome.py"
    assert result.tests is not None
    assert len(result.tests) > 0


def test_from_prompt_saves_to_workspace(autotester, mock_workspace):
    autotester.process_from_prompt(prompt=PROBLEM, output_path="test_palindrome.py")

    mock_workspace.write_file.assert_called_once_with(
        path="test_palindrome.py",
        content=result_tests(autotester, mock_workspace),
    )


def result_tests(autotester, mock_workspace):
    """Helper: capture the content written to workspace."""
    calls = mock_workspace.write_file.call_args_list
    assert calls, "write_file was not called"
    return calls[0].kwargs.get("content") or calls[0].args[1]


def test_from_prompt_calls_ai(autotester, mock_client):
    autotester.process_from_prompt(prompt=PROBLEM, output_path="test_output.py")
    mock_client.get_text.assert_called_once()


def test_from_prompt_includes_problem_in_ai_call(autotester, mock_client):
    autotester.process_from_prompt(prompt=PROBLEM, output_path="test_output.py")
    _, kwargs = mock_client.get_text.call_args
    messages = kwargs.get("messages") or mock_client.get_text.call_args[0][0]
    user_messages = [m for m in messages if m.get("role") == "user"]
    assert any(PROBLEM in m.get("content", "") for m in user_messages)


def test_from_prompt_on_save_tests_callback(autotester, mock_workspace):
    saved = []
    autotester.process_from_prompt(
        prompt=PROBLEM,
        output_path="test_output.py",
        on_save_tests=saved.append,
    )
    assert len(saved) == 1
    assert "def test_" in saved[0] or "assert" in saved[0]


def test_from_prompt_raises_on_empty_ai_response(mock_client, mock_environment, mock_workspace):
    from bizniz.autotester.autotester import Autotester

    empty_response = json.dumps({"tests": "", "notes": ""})
    mock_client.get_text.return_value = (
        empty_response,
        "job_id",
        [{"role": "assistant", "content": empty_response}],
    )

    at = Autotester(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=3,
    )
    with pytest.raises(AutotesterBadAIResponseError):
        at.process_from_prompt(prompt=PROBLEM, output_path="test_out.py")
