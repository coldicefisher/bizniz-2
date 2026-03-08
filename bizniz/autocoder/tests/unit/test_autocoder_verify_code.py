"""
The old _ai_verify_code step has been removed from the refactored Autocoder.
This file now covers the event emission (emit / on_event) behaviour.
"""
import json
import pytest
from unittest.mock import MagicMock

from bizniz.autocoder.types import AutocoderOnEventCallback
from bizniz.environment.types import ExecutionEnvironmentResult, ExecutionEnvironmentErrorDetails

from bizniz.autocoder.tests.conftest import make_get_text_response, VALID_GENERATE_JSON, VALID_REPAIR_JSON


def test_emit_calls_on_event_callback(autocoder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=1
    )
    events = []

    autocoder.generate(
        prompt="test",
        filename="test.py",
        on_event=events.append,
    )

    assert len(events) > 0
    assert all(isinstance(e, AutocoderOnEventCallback) for e in events)


def test_generate_stage_event_fired_on_success(autocoder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=1
    )
    events = []

    autocoder.generate(
        prompt="test",
        filename="test.py",
        on_event=events.append,
    )

    generate_events = [e for e in events if e.stage == "generate"]
    assert any(e.status == "success" for e in generate_events)


def test_repair_stage_event_fired_on_failure(mock_client, mock_environment, mock_workspace):
    repair_response = make_get_text_response(VALID_REPAIR_JSON)
    generate_response = make_get_text_response(VALID_GENERATE_JSON)

    mock_client.get_text.side_effect = [generate_response, repair_response]

    calls = {"n": 0}

    def fake_execute(code, call_spec):
        calls["n"] += 1
        if calls["n"] == 1:
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    type="RuntimeError", message="fail"
                ),
            )
        return ExecutionEnvironmentResult(success=True, result=0)

    mock_environment.execute.side_effect = fake_execute

    from bizniz.autocoder.autocoder import Autocoder
    autocoder = Autocoder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=3,
    )

    events = []
    autocoder.generate(
        prompt="test",
        filename="test.py",
        on_event=events.append,
    )

    repair_events = [e for e in events if e.stage == "repair"]
    assert any(e.status == "success" for e in repair_events)


def test_emit_no_callback_does_not_raise(autocoder, mock_environment):
    mock_environment.execute.return_value = ExecutionEnvironmentResult(
        success=True, result=0
    )
    # No on_event provided — should not raise
    autocoder.generate(prompt="test", filename="test.py")
