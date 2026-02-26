from pathlib import Path
import os
import shutil
import pytest

from autocoder.autocoder import AutocoderProcessError, AutocoderBadAIResponseError, AutocoderProcessResult, Autocoder, AutocoderConfig, AutocoderEnvironment

from autocoder.clients.chatgpt.chatgpt_client import ChatGPTClient

from typing import Optional, Callable, Any, Dict, List
from pydantic import ValidationError
from autocoder.tests.mock_validator import MockValidator


def test_autocoder_initialization_prompt(azure_config, validator_factory):
    '''
    This test verifies that the method `Autocoder._system_prompt` correctly generates the initialization prompt 
    based on the provided input data and process prompt. It checks that the generated prompt contains the necessary information 
    and follows the expected format.
    -- Should see the validator function in the prompt
    -- Should see the input data in the prompt
    -- Should see the process prompt in the prompt
    -- Should see the evaluation environment in the prompt
    -- Should see the additional libraries in the prompt
    '''
    Validator = validator_factory(True)
    
    autocoder = Autocoder(
        input_data="25, 17",
        process_prompt="Generate Python code to add numbers. You must figure out how to parse the input data and return the result.",
        max_retries=2,
        client=ChatGPTClient(config=azure_config),
        validator=MockValidator,
        config=AutocoderConfig(
            code_directory="/tmp/autocoder/code_generator"
        ),
    )
    
    initialization_prompt = autocoder._process_system_prompt.lower()
    print(initialization_prompt)
    
    assert isinstance(initialization_prompt, str)
    assert "Generate Python code to add numbers".lower() in initialization_prompt
    assert "You must figure out how to parse the input data and return the result.".lower() in initialization_prompt
    assert "The code will be wrapped in a function named `process(input_data: str)`".lower() in initialization_prompt
    assert "evaluation environment".lower() in initialization_prompt
    assert "additional libraries".lower() in initialization_prompt
    assert "validator.validate".lower() in initialization_prompt
    assert "`process(input_data: str)`".lower() in initialization_prompt
    assert "return".lower() in initialization_prompt
    assert "json".lower() in initialization_prompt
    assert "cannot_process".lower() in initialization_prompt
    assert "code".lower() in initialization_prompt
    