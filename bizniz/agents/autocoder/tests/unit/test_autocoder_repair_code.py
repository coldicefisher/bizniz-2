import json
import pytest
from unittest.mock import MagicMock

from bizniz.agents.autocoder.autocoder import Autocoder
from bizniz.agents.autocoder.types import AutocoderBadAIResponseError
from bizniz.environment.types import ExecutionCallSpec

from bizniz.agents.autocoder.tests.conftest import make_get_text_response, VALID_REPAIR_JSON


def test_repair_code_returns_code_and_call_spec(autocoder, mock_client):
    mock_client.get_text.return_value = make_get_text_response(VALID_REPAIR_JSON)

    code, call_spec = autocoder._repair_code(
        previous_code="broken_code()",
        error_message="NameError: broken_code is not defined",
    )

    assert "def add" in code
    assert isinstance(call_spec, ExecutionCallSpec)
    assert call_spec.symbol == "add"


def test_repair_code_adds_repair_prompt_to_history(autocoder, mock_client):
    mock_client.get_text.return_value = make_get_text_response(VALID_REPAIR_JSON)
    history_before = len(autocoder._message_history)

    autocoder._repair_code(
        previous_code="broken()",
        error_message="SyntaxError",
    )

    # Repair user message + assistant response added
    assert len(autocoder._message_history) > history_before


def test_repair_code_prompt_contains_error_and_code(autocoder, mock_client):
    mock_client.get_text.return_value = make_get_text_response(VALID_REPAIR_JSON)

    autocoder._repair_code(
        previous_code="print('old broken code')",
        error_message="TypeError: boom",
    )

    # Inspect the messages passed to the client
    call_kwargs = mock_client.get_text.call_args
    messages_passed = call_kwargs.kwargs.get("messages") or call_kwargs.args[0]

    all_content = " ".join(
        m.get("content", "") if isinstance(m, dict) else ""
        for m in messages_passed
    )
    assert "TypeError: boom" in all_content
    assert "print('old broken code')" in all_content


def test_repair_code_retries_on_invalid_json(autocoder, mock_client):
    good = make_get_text_response(VALID_REPAIR_JSON)

    mock_client.get_text.side_effect = [
        ("NOT JSON", "j1", []),
        good,
    ]

    code, _ = autocoder._repair_code(
        previous_code="broken",
        error_message="Error",
    )

    assert "def add" in code
    assert mock_client.get_text.call_count == 2


def test_repair_code_raises_after_exhausted_retries(autocoder, mock_client):
    mock_client.get_text.return_value = ("INVALID JSON", "j1", [])

    with pytest.raises(AutocoderBadAIResponseError):
        autocoder._repair_code(
            previous_code="broken",
            error_message="Error",
        )

    # Internal repair loop retries 3 times
    assert mock_client.get_text.call_count == 3


def test_repair_code_raises_on_empty_code(autocoder, mock_client):
    empty_code_response = {
        "code": "",
        "analysis": "nothing",
        "fix_plan": "nothing",
        "call_spec": {"symbol": "add", "args": [], "kwargs": {}},
    }
    mock_client.get_text.return_value = make_get_text_response(empty_code_response)

    with pytest.raises(AutocoderBadAIResponseError):
        autocoder._repair_code(
            previous_code="broken",
            error_message="Error",
        )
