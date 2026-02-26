from os import environ as env
import yaml
import os
import json
import pytest
from unittest.mock import MagicMock

from copy import deepcopy

from autocoder.clients.chatgpt.chatgpt_client import ChatGPTClient, ChatGPTClientConfig, AutocoderClientError
from autocoder.clients.chatgpt.messages import Message, MessageList
from autocoder.clients.chatgpt.types.roles import Role
from autocoder.clients.chatgpt.errors import OpenAIAuthError
from autocoder.base_validator import BaseValidator, ValidationResult

from openai import OpenAI, AzureOpenAI

from types import SimpleNamespace




@pytest.fixture
def openai_config():
    return ChatGPTClientConfig(
        is_azure=False,
        api_base=None,
        available_models=None,
        default_model=None,
        config_file_path=None,
    )


@pytest.fixture
def azure_config():
    return ChatGPTClientConfig(
        is_azure=True,
        api_base="https://example.openai.azure.com/",
        available_models={"gpt-4": "gpt-4"},
        default_model="gpt-4",
        api_version="2024-02-01",
        config_file_path=None,
    )


@pytest.fixture(autouse=True)
def mock_openai_clients(monkeypatch):
    monkeypatch.setattr(
        "autocoder.clients.chatgpt.chatgpt_client.OpenAI",
        MagicMock()
    )
    monkeypatch.setattr(
        "autocoder.clients.chatgpt.chatgpt_client.AzureOpenAI",
        MagicMock()
    )



@pytest.fixture
def mock_completion():
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content="Mocked response"
                )
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5
        )
    )


@pytest.fixture
def process_prompt():
    return "Generate Python code to add numbers. You must figure out how to parse the input data and return the result."


@pytest.fixture
def input_data():
    return "25, 17"


@pytest.fixture
def validator_factory():
    def _factory(is_valid=True):
        class MockValidator(BaseValidator):
            def validate(self, original_data: str, *args, **kwargs):
                self._original_data = original_data
                self._mutated_data = original_data
                return ValidationResult(is_valid=is_valid)
        return MockValidator
    return _factory



@pytest.fixture
def mock_chatgpt_client_factory():
    def _factory(response_json=None):
        mock_client = MagicMock(spec=ChatGPTClient)

        if response_json is None:
            response_json = {
                "cannot_process": False,
                "code": "```python\nprint(25 + 17)\n```"
            }

        mock_client.get_text.return_value = (
            json.dumps(response_json),
            "mock_job_id"
        )

        return mock_client
    return _factory



@pytest.fixture
def mock_verify_client_factory():
    def _factory(responses):
        """
        responses = list of values get_text should return in sequence
        Each value must be (text, job_id)
        """
        mock_client = MagicMock(spec=ChatGPTClient)
        mock_client.get_text.side_effect = responses
        return mock_client
    return _factory

