from os import environ as env
import yaml
import os
import json
import pytest
from unittest.mock import MagicMock
import pprint
from copy import deepcopy

from bizniz.clients.openai.chatgpt_client import ChatGPTClient, ChatGPTClientConfig, OpenAIClientError
from bizniz.clients.openai.messages import Message, MessageList
from bizniz.clients.openai.types.roles import Role
from bizniz.clients.openai.errors import OpenAIAuthError

from openai import OpenAI, AzureOpenAI

from types import SimpleNamespace


def test_chatgpt_client_azure_get_text_with_file_history(azure_config, mock_completion):
    print("Testing get_text with message history and callback...")
    messages = []
    received_message_history = []
    passed_message_history = None
    
    with open("test_message_history.json", "w") as f:
        f.write("""[
            {
                "role": "user",
                "content": "Previous message user"
            },
            {
                "role": "assistant",
                "content": "Previous response assistant"
            }
        ]""")
        
    with open("test_message_history.json", "r") as f:
        passed_message_history = json.load(f)
    
    def update_history(history):
        print(f"update_history called with: {history}")
        nonlocal received_message_history   
        received_message_history = history

    azure_client = ChatGPTClient(
        config=azure_config, 
        api_key="test",
        message_history_filepath="test_message_history.json",
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
    )

    for message in messages:
        print(f"Callback received message: role={message.role}, content={message.content}")
    
    
    print(f"Message history after update: {received_message_history}")
    # Ensure the correct message history was passed to the callback. The message history should include the previous messages plus the new user message, but not the system instruction message.
    expected_history = passed_message_history + [
        {
            "role": "user",
            "content": "Hello, how are you?"
        },
        {
            "role": "assistant",
            "content": "Mocked response"
        }
    ]
    print(f"Expected message history: {expected_history}")
    assert received_message_history == expected_history
    
    assert text == "Mocked response"
    assert job_id is not None
    # Open the file again and make sure the message history was saved correctly
    with open("test_message_history.json", "r") as f:
        saved_history = json.load(f)
    print(f"Saved message history in file: {saved_history}")
    assert saved_history == expected_history
    
    try:
        os.remove("test_message_history.json")
    except Exception as e:
        print(f"Failed to remove test message history file: {e}")


def test_chatgpt_client_openai_get_text_with_file_history(openai_config, mock_response):
    print("Testing get_text with message history and callback...")
    messages = []
    received_message_history = []
    passed_message_history = None
    
    with open("test_message_history.json", "w") as f:
        f.write("""[
            {
                "role": "user",
                "content": "Previous message user"
            },
            {
                "role": "assistant",
                "content": "Previous response assistant"
            }
        ]""")
        
    with open("test_message_history.json", "r") as f:
        passed_message_history = json.load(f)
    
    
    def update_history(history):
        print(f"update_history called with: {history}")
        nonlocal received_message_history   
        received_message_history = history

    openai_client = ChatGPTClient(
        config=openai_config, 
        api_key="test",
        message_history_filepath="test_message_history.json",
        on_message_callback=lambda message: messages.append(message),
        on_message_history_update_callback=update_history
    )
    
    assert openai_client.config.is_azure is False
    assert openai_client.config.api_base == "https://api.openai.com/v1/"
    assert "gpt-3.5-turbo" in openai_client.config.available_models
    assert openai_client.config.default_model == "gpt-3.5-turbo"

    # Mock the OpenAI Responses API
    openai_client._ai_agent.responses = MagicMock()
    openai_client._ai_agent.responses.create = MagicMock(
        return_value=mock_response
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

    for message in messages:
        print(f"Callback received message: role={message.role}, content={message.content}")
    
    
    print(f"Message history after update: {received_message_history}")
    # Ensure the correct message history was passed to the callback. The message history should include the previous messages plus the new user message, but not the system instruction message.
    expected_history = passed_message_history + [
        {
            "role": "user",
            "content": "Hello, how are you?"
        },
        {
            "role": "assistant",
            "content": "Mocked response"
        }
        
    ]
    print(f"Expected message history: {expected_history}")
    assert received_message_history == expected_history
    
    assert text == "Mocked response"
    assert job_id is not None

    # Open the file again and make sure the message history was saved correctly
    with open("test_message_history.json", "r") as f:
        saved_history = json.load(f)
    print(f"Saved message history in file: {saved_history}")
    assert saved_history == expected_history
    
    try:
        os.remove("test_message_history.json")
    except Exception as e:
        print(f"Failed to remove test message history file: {e}")