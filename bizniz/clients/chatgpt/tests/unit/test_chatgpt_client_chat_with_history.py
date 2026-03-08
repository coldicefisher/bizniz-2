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


def test_chatgpt_client_azure_get_text_with_history(azure_config, mock_completion):
    
    messages = []
    received_message_history = []
    passed_message_history = [
            {
                "role": Role.USER.value,
                "content": "Previous message user"
            },
            {
                "role": Role.ASSISTANT.value,
                "content": "Previous response assistant"
            }
        ]
    
    def update_history(history):
        print(f"update_history called with: {history}")
        nonlocal received_message_history   
        received_message_history = history

    azure_client = ChatGPTClient(
        config=azure_config, 
        api_key="test",
        message_history=deepcopy(passed_message_history),
        on_message_callback=lambda message: messages.append(message),
        on_message_history_update_callback=update_history
    )
    
    assert azure_client.config.is_azure is True
    assert azure_client.config.api_base == "https://example.openai.azure.com/"
    assert "gpt-4" in azure_client.config.available_models
    assert azure_client.config.default_model == "gpt-4"
    
    # Test the messages
    assert azure_client._message_history is not None
    assert len(azure_client._message_history) == 2
    assert azure_client._message_history[0].get('role') == Role.USER.value
    assert azure_client._message_history[0].get('content') == "Previous message user"
    assert azure_client._message_history[1].get('role') == Role.ASSISTANT.value
    assert azure_client._message_history[1].get('content') == "Previous response assistant"
    
    
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
        use_message_history=True,
    )
    print("New history after get_text call:", azure_client._message_history)
    # Ensure the correct message history was passed to the callback. The message history should include the previous messages plus the new user message, but not the system instruction message.
    expected_history = passed_message_history + [
        {
            "role": Role.USER.value,
            "content": "Hello, how are you?"
        },
        {
            "role": Role.ASSISTANT.value,
            "content": "Mocked response"
        }
    ]
    
    assert received_message_history == expected_history
    
    assert text == "Mocked response"
    assert job_id is not None


def test_chatgpt_client_openai_get_text_with_history(openai_config, mock_completion):
    messages = []
    received_message_history = []
    passed_message_history = [
            {
                "role": Role.USER.value,
                "content": "Previous message user"
            },
            {
                "role": Role.ASSISTANT.value,
                "content": "Previous response assistant"
            }
        ]
    
    def update_history(history):
        
        nonlocal received_message_history   
        received_message_history = history

    openai_client = ChatGPTClient(
        config=openai_config, 
        api_key="test",
        message_history=deepcopy(passed_message_history),
        on_message_callback=lambda message: messages.append(message),
        on_message_history_update_callback=update_history
    )
    
    assert openai_client.config.is_azure is False
    assert openai_client.config.api_base == "https://api.openai.com/v1/"
    assert "gpt-3.5-turbo" in openai_client.config.available_models
    assert openai_client.config.default_model == "gpt-3.5-turbo"

    # Mock the Azure client chain
    openai_client._ai_agent.responses = MagicMock()
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
        use_message_history=True,
    )

    # Ensure the correct message history was passed to the callback. The message history should include the previous messages plus the new user message, but not the system instruction message.
    expected_history = passed_message_history + [
        {
            "role": Role.USER.value,
            "content": "Hello, how are you?"
        },
        {
            "role": Role.ASSISTANT.value,
            "content": "Mocked response"
        }
    ]
    assert received_message_history == expected_history
    
    assert text == "Mocked response"
    assert job_id is not None




def test_chatgpt_client_azure_get_text_with_history_passed_to_get_text(azure_config, mock_completion):
    
    messages = []
    received_message_history = []
    passed_message_history = [
            {
                "role": Role.USER.value,
                "content": "Previous message user"
            },
            {
                "role": Role.ASSISTANT.value,
                "content": "Previous response assistant"
            }
        ]
    
    def update_history(history):
        print(f"update_history called with: {history}")
        nonlocal received_message_history   
        received_message_history = history

    azure_client = ChatGPTClient(
        config=azure_config, 
        api_key="test",
        # message_history=deepcopy(passed_message_history),
        on_message_callback=lambda message: messages.append(message),
        on_message_history_update_callback=update_history
    )
    
    assert azure_client.config.is_azure is True
    assert azure_client.config.api_base == "https://example.openai.azure.com/"
    assert "gpt-4" in azure_client.config.available_models
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
        use_message_history=True,
        message_history=deepcopy(passed_message_history)
    )
    print("New history after get_text call:", azure_client._message_history)
    # Ensure the correct message history was passed to the callback. The message history should include the previous messages plus the new user message, but not the system instruction message.
    expected_history = passed_message_history + [
        {
            "role": Role.USER.value,
            "content": "Hello, how are you?"
        },
        {
            "role": Role.ASSISTANT.value,
            "content": "Mocked response"
        }
    ]
    
    assert received_message_history == expected_history
    
    assert text == "Mocked response"
    assert job_id is not None


def test_chatgpt_client_openai_get_text_with_history_passed_to_get_text(openai_config, mock_completion):
    messages = []
    received_message_history = []
    passed_message_history = [
            {
                "role": Role.USER.value,
                "content": "Previous message user"
            },
            {
                "role": Role.ASSISTANT.value,
                "content": "Previous response assistant"
            }
        ]
    
    def update_history(history):
        
        nonlocal received_message_history   
        received_message_history = history

    openai_client = ChatGPTClient(
        config=openai_config, 
        api_key="test",
        # message_history=deepcopy(passed_message_history),
        on_message_callback=lambda message: messages.append(message),
        on_message_history_update_callback=update_history
    )
    
    assert openai_client.config.is_azure is False
    assert openai_client.config.api_base == "https://api.openai.com/v1/"
    assert "gpt-3.5-turbo" in openai_client.config.available_models
    assert openai_client.config.default_model == "gpt-3.5-turbo"

    # Mock the Azure client chain
    openai_client._ai_agent.responses = MagicMock()
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
        use_message_history=True,
        message_history=deepcopy(passed_message_history)
    )

    # Ensure the correct message history was passed to the callback. The message history should include the previous messages plus the new user message, but not the system instruction message.
    expected_history = passed_message_history + [
        {
            "role": Role.USER.value,
            "content": "Hello, how are you?"
        },
        {
            "role": Role.ASSISTANT.value,
            "content": "Mocked response"
        }
    ]
    assert received_message_history == expected_history
    
    assert text == "Mocked response"
    assert job_id is not None
