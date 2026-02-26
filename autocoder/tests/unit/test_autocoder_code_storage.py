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



def test_retrieve_saved_code_with_explicit_filename(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    config = AutocoderConfig(
        code_directory=str(tmp_path),
        module_name="code",
        filename="generated_code.py"
    )

    autocoder = Autocoder(
        input_data="x",
        process_prompt="test",
        validator=Validator(),
        client=mock_client,
        config=config,
    )

    # Correct module path
    module_dir = tmp_path / "code"
    file_path = module_dir / "generated_code.py"
    file_path.write_text("print('hello')")

    content, filename = autocoder.retrieve_saved_code()

    assert content == "print('hello')"
    assert filename == "generated_code.py"


def test_save_code_creates_file(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    config = AutocoderConfig(
        code_directory=str(tmp_path),
        module_name="code",
        filename="generated_code.py"
    )

    autocoder = Autocoder(
        input_data="x",
        process_prompt="test",
        validator=Validator(),
        client=mock_client,
        config=config,
    )

    autocoder._save_code_to_file("print('new')")

    module_dir = tmp_path / "code"
    file_path = module_dir / "generated_code.py"


    assert file_path.exists()
    assert file_path.read_text() == "print('new')"


def test_save_code_moves_existing_to_cache(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    config = AutocoderConfig(
        code_directory=str(tmp_path),
        module_name="code",
        filename="generated_code.py"
    )

    autocoder = Autocoder(
        input_data="x",
        process_prompt="test",
        validator=Validator(),
        client=mock_client,
        config=config,
    )

    # Correct module directory
    module_dir = tmp_path / "code"

    # Create existing file
    original_file = module_dir / "generated_code.py"
    original_file.write_text("old code")

    # Save new code
    autocoder._save_code_to_file("new code")

    # New file overwritten
    assert original_file.exists()
    assert original_file.read_text() == "new code"

    # Cached file exists inside module_dir/cached
    cache_dir = module_dir / "cached"
    assert cache_dir.exists()

    cached_files = list(cache_dir.glob("*generated_code.py"))
    assert len(cached_files) == 1
    assert cached_files[0].read_text() == "old code"

def test_save_code_sanitizes_filename(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    config = AutocoderConfig(
        code_directory=str(tmp_path),
        module_name="code",
        filename="bad:name?.py"
    )

    autocoder = Autocoder(
        input_data="x",
        process_prompt="test",
        validator=Validator(),
        client=mock_client,
        config=config,
    )

    autocoder._save_code_to_file("content")

    sanitized = "bad_name_.py"

    module_dir = tmp_path / "code"
    assert (module_dir / sanitized).exists()




def test_strip_code_block_plain_text(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    autocoder = Autocoder(
        input_data="x",
        process_prompt="test",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
    )

    text = "print('hello')"
    assert autocoder._strip_code_block(text) == "print('hello')"


def test_strip_code_block_python(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    autocoder = Autocoder(
        input_data="x",
        process_prompt="test",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
    )

    text = "```python\nprint('hello')\n```"

    result = autocoder._strip_code_block(text)

    assert result == "print('hello')"


def test_strip_code_block_generic(tmp_path, validator_factory, mock_chatgpt_client_factory):
    Validator = validator_factory(True)
    mock_client = mock_chatgpt_client_factory()

    autocoder = Autocoder(
        input_data="x",
        process_prompt="test",
        validator=Validator(),
        client=mock_client,
        config=AutocoderConfig(code_directory=str(tmp_path)),
    )

    text = "```\nprint('hello')\n```"

    result = autocoder._strip_code_block(text)

    assert result == "print('hello')"


