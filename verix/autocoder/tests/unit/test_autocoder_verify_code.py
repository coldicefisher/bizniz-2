from pathlib import Path
import os
import shutil
import pytest
import json

from autocoder.autocoder import AutocoderProcessError, AutocoderBadAIResponseError, AutocoderProcessResult, Autocoder, AutocoderConfig, AutocoderEnvironment

from autocoder.clients.chatgpt.openai_chatgpt_client import ChatGPTClient

from typing import Optional, Callable, Any, Dict, List
from pydantic import ValidationError
from autocoder.tests.mock_validator import MockValidator
from unittest.mock import MagicMock





def test_ai_verify_code_success(tmp_path, validator_factory, mock_verify_client_factory):
    Validator = validator_factory(True)

    responses = [
        (json.dumps({"is_valid": True}), "job1")
    ]

    mock_client = mock_verify_client_factory(responses)

    autocoder = Autocoder(
        input_data="25,17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
    )

    result = autocoder._ai_verify_code(
        code="print(25+17)",
        input_data="25,17",
        output="42"
    )

    assert result is True
    assert mock_client.get_text.call_count == 1




def test_ai_verify_code_returns_false(tmp_path, validator_factory, mock_verify_client_factory):
    Validator = validator_factory(True)

    responses = [
        (json.dumps({"is_valid": False}), "job1")
    ]

    mock_client = mock_verify_client_factory(responses)

    autocoder = Autocoder(
        input_data="25,17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
    )

    result = autocoder._ai_verify_code(
        code="print(25+17)",
        input_data="25,17",
        output="42"
    )

    assert result is False



def test_ai_verify_code_retries_on_invalid_json(tmp_path, validator_factory, mock_verify_client_factory):
    Validator = validator_factory(True)

    responses = [
        ("not json", "job1"),
        (json.dumps({"is_valid": True}), "job2")
    ]

    mock_client = mock_verify_client_factory(responses)

    autocoder = Autocoder(
        input_data="25,17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
    )

    result = autocoder._ai_verify_code(
        code="print(25+17)",
        input_data="25,17",
        output="42"
    )

    assert result is True
    assert mock_client.get_text.call_count == 2



def test_ai_verify_code_exhausts_retries(tmp_path, validator_factory, mock_verify_client_factory):
    Validator = validator_factory(True)

    responses = [
        ("bad json", "job1"),
        ("still bad", "job2"),
        ("nope", "job3")
    ]

    mock_client = mock_verify_client_factory(responses)

    autocoder = Autocoder(
        input_data="25,17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
    )

    with pytest.raises(AutocoderBadAIResponseError):
        autocoder._ai_verify_code(
            code="print(25+17)",
            input_data="25,17",
            output="42"
        )

    assert mock_client.get_text.call_count == autocoder.max_retries



def test_ai_verify_code_prompt_content(tmp_path, validator_factory, mock_verify_client_factory):
    Validator = validator_factory(True)

    responses = [
        (json.dumps({"is_valid": True}), "job1")
    ]

    mock_client = mock_verify_client_factory(responses)

    autocoder = Autocoder(
        input_data="25,17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
    )

    autocoder._ai_verify_code(
        code="print(25+17)",
        input_data="25,17",
        output="42"
    )

    call_args = mock_client.get_text.call_args

    instruction_messages = call_args.kwargs["instruction_messages"]
    user_messages = call_args.kwargs["messages"]

    assert "42" in instruction_messages[0]["content"]
    assert "25,17" in instruction_messages[0]["content"]
