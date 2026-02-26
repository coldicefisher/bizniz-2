from pathlib import Path
import os
import shutil
import pytest
import json

from autocoder.autocoder import AutocoderProcessError, AutocoderBadAIResponseError, AutocoderProcessResult, Autocoder, AutocoderConfig, AutocoderEnvironment

from autocoder.clients.chatgpt.chatgpt_client import ChatGPTClient

from typing import Optional, Callable, Any, Dict, List
from pydantic import ValidationError
from autocoder.tests.mock_validator import MockValidator
from unittest.mock import MagicMock




def test_repair_code_success(tmp_path, validator_factory):
    Validator = validator_factory(True)

    mock_client = MagicMock()
    mock_client.get_text.return_value = (
        json.dumps({
            "cannot_process": False,
            "code": "```python\nprint(42)\n```"
        }),
        "job123"
    )

    autocoder = Autocoder(
        input_data="25, 17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        max_retries=3,
        config=AutocoderConfig(code_directory=str(tmp_path))
    )

    repaired = autocoder._repair_code(
        previous_code="print('broken')",
        error_message="SyntaxError"
    )

    assert repaired == "print(42)"
    assert mock_client.get_text.call_count == 1


def test_repair_code_retry_on_invalid_json(tmp_path, validator_factory):
    Validator = validator_factory(True)

    mock_client = MagicMock()
    mock_client.get_text.side_effect = [
        ("NOT JSON", "job1"),
        (json.dumps({
            "cannot_process": False,
            "code": "print(99)"
        }), "job2")
    ]

    autocoder = Autocoder(
        input_data="25, 17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        max_retries=3,
        config=AutocoderConfig(code_directory=str(tmp_path))
    )

    repaired = autocoder._repair_code(
        previous_code="broken",
        error_message="Error"
    )

    assert repaired == "print(99)"
    assert mock_client.get_text.call_count == 2


def test_repair_code_cannot_process(tmp_path, validator_factory):
    Validator = validator_factory(True)

    mock_client = MagicMock()
    mock_client.get_text.return_value = (
        json.dumps({
            "cannot_process": True
        }),
        "job1"
    )

    autocoder = Autocoder(
        input_data="25, 17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        max_retries=2,
        config=AutocoderConfig(code_directory=str(tmp_path))
    )

    with pytest.raises(AutocoderProcessError):
        autocoder._repair_code(
            previous_code="broken",
            error_message="Error"
        )


def test_repair_code_exhausts_retries(tmp_path, validator_factory):
    Validator = validator_factory(True)

    mock_client = MagicMock()
    mock_client.get_text.return_value = ("INVALID JSON", "job1")

    autocoder = Autocoder(
        input_data="25, 17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        max_retries=2,
        config=AutocoderConfig(code_directory=str(tmp_path))
    )

    with pytest.raises(AutocoderBadAIResponseError):
        autocoder._repair_code(
            previous_code="broken",
            error_message="Error"
        )

    assert mock_client.get_text.call_count == 2


def test_repair_prompt_contents(tmp_path, validator_factory):
    Validator = validator_factory(True)

    mock_client = MagicMock()
    mock_client.get_text.return_value = (
        json.dumps({
            "cannot_process": False,
            "code": "print(1)"
        }),
        "job1"
    )

    autocoder = Autocoder(
        input_data="25, 17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        max_retries=1,
        config=AutocoderConfig(code_directory=str(tmp_path))
    )

    autocoder._repair_code(
        previous_code="print('broken')",
        error_message="SyntaxError"
    )

    args, kwargs = mock_client.get_text.call_args

    instruction_messages = kwargs["instruction_messages"]
    repair_prompt = instruction_messages[0]["content"]

    assert "SyntaxError" in repair_prompt
    assert "print('broken')" in repair_prompt
    assert "previously generated python code failed" in repair_prompt.lower()
