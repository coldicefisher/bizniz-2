from os import environ as env
import yaml
import os
import json
import pytest
from unittest.mock import MagicMock

from copy import deepcopy

from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient, ChatGPTClientConfig, AutocoderClientError
from bizniz.clients.chatgpt.messages import Message, MessageList
from bizniz.clients.chatgpt.types.roles import Role
from bizniz.clients.chatgpt.errors import OpenAIAuthError

from openai import OpenAI, AzureOpenAI

from types import SimpleNamespace



def test_chatgpt_client_azure_get_text(azure_config, mock_completion):
    azure_client = ChatGPTClient(config=azure_config, api_key="test")
    assert azure_client.config.is_azure is True
    assert azure_client.config.api_base == "https://example.openai.azure.com/"
    assert azure_client.config.available_models == {"gpt-4": "gpt-4"}
    assert azure_client.config.default_model == "gpt-4"

    # Mock the Azure client chain
    azure_client._ai_agent.chat = MagicMock()
    azure_client._ai_agent.chat.completions = MagicMock()
    azure_client._ai_agent.chat.completions.create = MagicMock(
        return_value=mock_completion
    )

    text, job_id, output_messages = azure_client.get_text(
        instruction_messages=[{
            "role": Role.SYSTEM.value,
            "content": "You are a helpful assistant."
        }],
        messages=[{
            "role": Role.USER.value,
            "content": "Hello, how are you?"
        }],
    )

    assert text == "Mocked response"
    assert job_id is not None


def test_chatgpt_client_openai_get_text(openai_config, mock_completion):
    openai_client = ChatGPTClient(config=openai_config, api_key="test")
    assert openai_client.config.is_azure is False
    assert openai_client._config.is_azure is False
    assert openai_client._config.api_base == "https://api.openai.com/v1/"
    assert openai_client._config.default_model == "gpt-3.5-turbo"
    assert "gpt-3.5-turbo" in openai_client._config.available_models


    # Mock the Azure client chain
    openai_client._ai_agent.chat = MagicMock()
    openai_client._ai_agent.responses.create = MagicMock()
    openai_client._ai_agent.responses.create = MagicMock(
        return_value=mock_completion
    )

    text, job_id, output_messages = openai_client.get_text(
        instruction_messages=[{
            "role": Role.SYSTEM.value,
            "content": "You are a helpful assistant."
        }],
        messages=[{
            "role": Role.USER.value,
            "content": "Hello, how are you?"
        }],
    )

    
    assert text == "Mocked response"
    assert job_id is not None


