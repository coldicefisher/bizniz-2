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


def test_process_short_circuit(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    def native_filter(x):
        return True

    autocoder = Autocoder(
        input_data="raw_input",
        process_prompt="irrelevant",
        validator=Validator(),
        client=mock_client,
        process_filter_function=native_filter,
        config=AutocoderConfig(code_directory=str(tmp_path))
    )

    result = autocoder.process()

    assert result.cannot_process is False
    assert result.output == "raw_input"
    assert result.code is None
    mock_client.get_text.assert_not_called()



def test_process_generate_success(monkeypatch, tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    autocoder = Autocoder(
        input_data="5,7",
        process_prompt="Add",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path))
    )

    # Mock generate
    monkeypatch.setattr(
        autocoder,
        "_generate_code",
        lambda system_prompt: "def process(input_data: str): return 12"
    )
    monkeypatch.setattr(
        "autocoder.autocoder.evaluate_generated_code",
        lambda **kwargs: {"success": True, "result": 12}
    )

    result = autocoder.process()

    assert result.cannot_process is False
    assert result.output == 12
    assert "def process" in result.code



def test_process_repair_flow(monkeypatch, tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    autocoder = Autocoder(
        input_data="bad",
        process_prompt="Add",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
        max_retries=2
    )

    monkeypatch.setattr(
        autocoder,
        "_generate_code",
        lambda system_prompt: "broken_code"
    )

    # First evaluation fails
    calls = {"count": 0}
    def fake_eval(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return {"success": False, "result": "SyntaxError"}
        return {"success": True, "result": 42}

    monkeypatch.setattr(
        "autocoder.autocoder.evaluate_generated_code",
        fake_eval
    )


    monkeypatch.setattr(
        autocoder,
        "_repair_code",
        lambda previous_code, error_message: "fixed_code"
    )

    result = autocoder.process()

    assert result.output == 42
    assert calls["count"] == 2



def test_process_ai_verification_failure(monkeypatch, tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    autocoder = Autocoder(
        input_data="x",
        process_prompt="Add",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
        max_retries=2
    )

    monkeypatch.setattr(
        autocoder,
        "_generate_code",
        lambda system_prompt: "def process(input_data: str): return 5"
    )

    monkeypatch.setattr(
        "autocoder.autocoder.evaluate_generated_code",
        lambda **kwargs: {"success": True, "result": 5}
    )

    # First verification fails
    calls = {"count": 0}
    def fake_verify(code, input_data, output):
        calls["count"] += 1
        return calls["count"] > 1

    monkeypatch.setattr(autocoder, "_ai_verify_code", fake_verify)
    monkeypatch.setattr(autocoder, "_repair_code", lambda previous_code, error_message: "fixed")

    result = autocoder.process(ai_verification=True)

    assert calls["count"] == 2
    assert result.cannot_process is False



def test_process_exhausted_retries(monkeypatch, tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(False)
    mock_client = mock_chatgpt_client_factory()

    autocoder = Autocoder(
        input_data="x",
        process_prompt="Add",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
        max_retries=1
    )

    monkeypatch.setattr(
        autocoder,
        "_generate_code",
        lambda system_prompt: "broken"
    )

    monkeypatch.setattr(
        "autocoder.autocoder.evaluate_generated_code",
        lambda **kwargs: {"success": False, "result": "error"}
    )

    monkeypatch.setattr(
        autocoder,
        "_repair_code",
        lambda previous_code, error_message: "still broken"
    )

    import pytest
    with pytest.raises(AutocoderProcessError):
        autocoder.process()
