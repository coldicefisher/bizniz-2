from os import environ as env
import yaml
import os
import json
import pytest
from unittest.mock import MagicMock

from copy import deepcopy

from bizniz.clients.openai.chatgpt_client import ChatGPTClient, ChatGPTClientConfig, AutocoderClientError
from bizniz.clients.openai.messages import Message, MessageList
from bizniz.clients.openai.types.roles import Role
from bizniz.clients.openai.errors import OpenAIAuthError

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
        "bizniz.clients.openai.chatgpt_client.OpenAI",
        MagicMock()
    )
    monkeypatch.setattr(
        "bizniz.clients.openai.chatgpt_client.AzureOpenAI",
        MagicMock()
    )



@pytest.fixture
def mock_completion():
    """Mock for Chat Completions API (Azure)."""
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
def mock_response():
    """Mock for Responses API (OpenAI)."""
    return SimpleNamespace(
        output_text="Mocked response",
        output=[
            SimpleNamespace(
                type="message",
                role="assistant",
                content=[SimpleNamespace(type="output_text", text="Mocked response")]
            )
        ],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5
        )
    )

