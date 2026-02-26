from pathlib import Path
import os
import shutil
import pytest

from autocoder.autocoder import AutocoderProcessError, AutocoderBadAIResponseError, AutocoderProcessResult, Autocoder, AutocoderConfig, AutocoderEnvironment

from autocoder.clients.chatgpt.chatgpt_client import ChatGPTClient

from typing import Optional, Callable, Any, Dict, List
from pydantic import ValidationError
from autocoder.tests.mock_validator import MockValidator
from unittest.mock import MagicMock



def test_generate_code_success(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    autocoder = Autocoder(
        input_data="25, 17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(
            code_directory=str(tmp_path)
        )
    )

    code = autocoder._generate_code(
        system_prompt="Generate code",
        messages=[]
    )

    assert "print(25 + 17)" in code
    mock_client.get_text.assert_called_once()
    
    


def test_generate_code_cannot_process(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)

    mock_client = mock_chatgpt_client_factory({
        "cannot_process": True,
        "code": ""
    })

    autocoder = Autocoder(
        input_data="25, 17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(
            code_directory=str(tmp_path)
        )
    )

    with pytest.raises(AutocoderProcessError):
        autocoder._generate_code(
            system_prompt="Generate code",
            messages=[]
        )
    
    
def test_generate_code_cannot_process(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)

    mock_client = mock_chatgpt_client_factory({
        "cannot_process": True,
        "code": ""
    })

    autocoder = Autocoder(
        input_data="25, 17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(
            code_directory=str(tmp_path)
        )
    )

    with pytest.raises(AutocoderProcessError):
        autocoder._generate_code(
            system_prompt="Generate code",
            messages=[]
        )

def test_generate_code_invalid_json(tmp_path, validator_factory):
    Validator = validator_factory(True)

    mock_client = MagicMock(spec=ChatGPTClient)
    mock_client.get_text.return_value = ("not json", "job_id")

    autocoder = Autocoder(
        input_data="25, 17",
        process_prompt="Add numbers",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(
            code_directory=str(tmp_path)
        )
    )

    with pytest.raises(AutocoderBadAIResponseError):
        autocoder._generate_code(
            system_prompt="Generate code",
            messages=[]
        )

