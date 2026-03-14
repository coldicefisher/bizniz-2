import json
import pytest
from unittest.mock import MagicMock

from bizniz.agents.autocoder.autocoder import Autocoder
from bizniz.agents.autocoder.types import AutocoderBadAIResponseError
from bizniz.clients.chatgpt.messages import Message
from bizniz.environment.types import ExecutionCallSpec

from bizniz.agents.autocoder.tests.conftest import make_get_text_response, VALID_GENERATE_JSON


def test_generate_code_returns_code_and_call_spec(autocoder):
    code, call_spec = autocoder._generate_code(
        messages=[Message(role="user", content="Add two numbers")]
    )

    assert "def add" in code
    assert isinstance(call_spec, ExecutionCallSpec)
    assert call_spec.symbol == "add"


def test_generate_code_calls_client_once(autocoder, mock_client):
    autocoder._generate_code(
        messages=[Message(role="user", content="test")]
    )

    mock_client.get_text.assert_called_once()


def test_generate_code_adds_messages_to_history(autocoder):
    history_before = len(autocoder._message_history)

    autocoder._generate_code(
        messages=[Message(role="user", content="test")]
    )

    # User message + assistant response added
    assert len(autocoder._message_history) > history_before


def test_generate_code_strips_code_block(autocoder, mock_client):
    response_with_fence = {
        "code": "```python\ndef add(a, b): return a + b\n```",
        "call_spec": {"symbol": "add", "args": [], "kwargs": {}},
    }
    mock_client.get_text.return_value = make_get_text_response(response_with_fence)

    code, _ = autocoder._generate_code(
        messages=[Message(role="user", content="test")]
    )

    assert "```" not in code
    assert "def add" in code


def test_generate_code_raises_on_all_bad_responses(autocoder, mock_client):
    mock_client.get_text.return_value = ("NOT VALID JSON", "job_id", [])

    with pytest.raises(AutocoderBadAIResponseError):
        autocoder._generate_code(
            messages=[Message(role="user", content="test")]
        )


def test_generate_code_retries_on_empty_response(autocoder, mock_client):
    good_response = make_get_text_response(VALID_GENERATE_JSON)

    mock_client.get_text.side_effect = [
        ("", "job1", []),
        good_response,
    ]

    code, call_spec = autocoder._generate_code(
        messages=[Message(role="user", content="test")]
    )

    assert "def add" in code
    assert mock_client.get_text.call_count == 2
