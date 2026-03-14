import json
import pytest
from unittest.mock import MagicMock, call

from bizniz.agents.autocoder.autocoder import Autocoder
from bizniz.agents.autocoder.types import AutocoderProcessError, AutocoderProcessResult
from bizniz.environment.types import ExecutionEnvironmentResult, ExecutionEnvironmentErrorDetails

from bizniz.agents.autocoder.tests.conftest import make_get_text_response, VALID_GENERATE_JSON, VALID_REPAIR_JSON


def test_process_success_returns_result(autocoder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=42
    )

    result = autocoder.generate(prompt="Add numbers", filename="add.py")

    assert isinstance(result, AutocoderProcessResult)
    assert result.output == 42
    assert result.changes is not None


def test_process_saves_code_to_workspace(autocoder, mock_environment, mock_workspace):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=42
    )

    autocoder.generate(prompt="Add numbers", filename="add.py")

    mock_workspace.path.assert_called()


def test_process_calls_on_save_code_callback(autocoder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=42
    )
    on_save_code = MagicMock()

    autocoder.generate(
        prompt="Add numbers",
        filename="add.py",
        on_save_code=on_save_code,
    )

    on_save_code.assert_called_once()
    saved_code = on_save_code.call_args[0][0]
    assert "def add" in saved_code


def test_process_repairs_on_first_failure(mock_client, mock_environment, mock_workspace):
    repair_response = make_get_text_response(VALID_REPAIR_JSON)
    generate_response = make_get_text_response(VALID_GENERATE_JSON)

    # First call: generate, subsequent calls: repair
    mock_client.get_text.side_effect = [generate_response, repair_response]

    calls = {"count": 0}

    def fake_execute(code, call_spec):
        calls["count"] += 1
        if calls["count"] == 1:
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    type="RuntimeError", message="broken"
                ),
            )
        return ExecutionEnvironmentResult(success=True, result=99)

    mock_environment.execute.side_effect = fake_execute

    autocoder = Autocoder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=3,
    )

    result = autocoder.generate(prompt="Fix me", filename="fix.py")

    assert result.output == 99
    assert calls["count"] == 2


def test_process_raises_after_exhausted_retries(mock_client, mock_environment, mock_workspace):
    repair_response = make_get_text_response(VALID_REPAIR_JSON)
    generate_response = make_get_text_response(VALID_GENERATE_JSON)

    mock_client.get_text.side_effect = [generate_response] + [repair_response] * 10

    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=False,
        error=ExecutionEnvironmentErrorDetails(
            type="RuntimeError", message="always fails"
        ),
    )

    autocoder = Autocoder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=2,
    )

    with pytest.raises(AutocoderProcessError):
        autocoder.generate(prompt="Broken", filename="broken.py")


def test_process_loads_existing_code_from_workspace(mock_client, mock_environment, mock_workspace):
    mock_workspace.exists.return_value = True
    mock_workspace.read_file.return_value = "# cached code"
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=1
    )

    autocoder = Autocoder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
    )

    autocoder.generate(prompt="Do stuff", filename="cached.py")

    mock_workspace.read_file.assert_called_once()


def test_process_fires_status_messages(autocoder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=0
    )
    messages = []

    autocoder.generate(
        prompt="test",
        filename="test.py",
        on_status_message=messages.append,
    )

    assert len(messages) > 0


def test_process_fires_on_event(autocoder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=0
    )
    events = []

    autocoder.generate(
        prompt="test",
        filename="test.py",
        on_event=events.append,
    )

    assert len(events) > 0
