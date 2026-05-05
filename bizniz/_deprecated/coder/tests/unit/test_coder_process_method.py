import json
import pytest
from unittest.mock import MagicMock, call

from bizniz.agents.coder.coder import Coder
from bizniz.agents.coder.types import CoderProcessError, CoderProcessResult
from bizniz.environment.types import ExecutionEnvironmentResult, ExecutionEnvironmentErrorDetails

from bizniz.agents.coder.tests.conftest import make_get_text_response, VALID_GENERATE_JSON, VALID_REPAIR_JSON


def test_process_success_returns_result(coder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=42
    )

    result = coder.generate(prompt="Add numbers", filename="add.py")

    assert isinstance(result, CoderProcessResult)
    assert result.output == 42
    assert result.changes is not None


def test_process_saves_code_to_workspace(coder, mock_environment, mock_workspace):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=42
    )

    coder.generate(prompt="Add numbers", filename="add.py")

    mock_workspace.path.assert_called()


def test_process_calls_on_save_code_callback(coder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=42
    )
    on_save_code = MagicMock()

    coder.generate(
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

    coder = Coder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=3,
    )

    result = coder.generate(prompt="Fix me", filename="fix.py")

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

    coder = Coder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=2,
    )

    with pytest.raises(CoderProcessError):
        coder.generate(prompt="Broken", filename="broken.py")


def test_process_loads_existing_code_from_workspace(mock_client, mock_environment, mock_workspace):
    mock_workspace.exists.return_value = True
    mock_workspace.read_file.return_value = "# cached code"
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=1
    )

    coder = Coder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
    )

    coder.generate(prompt="Do stuff", filename="cached.py")

    mock_workspace.read_file.assert_called_once()


def test_process_fires_status_messages(coder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=0
    )
    messages = []

    coder.generate(
        prompt="test",
        filename="test.py",
        on_status_message=messages.append,
    )

    assert len(messages) > 0


def test_process_fires_on_event(coder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=0
    )
    events = []

    coder.generate(
        prompt="test",
        filename="test.py",
        on_event=events.append,
    )

    assert len(events) > 0
