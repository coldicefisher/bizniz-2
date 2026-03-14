from os import environ as env
import yaml
import os
import json
import pytest
from unittest.mock import MagicMock

from bizniz.clients.openai.chatgpt_client import ChatGPTClient, ChatGPTClientConfig, AutocoderClientError
from bizniz.clients.openai.messages import Message, MessageList
from bizniz.clients.openai.types.roles import Role
from bizniz.clients.openai.errors import OpenAIAuthError

from openai import OpenAI, AzureOpenAI



def test_initialize_api_key_from_environment_with_openai(monkeypatch, azure_config):
    """Test initializing ChatGPTClient with API key from environment. """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    
    with pytest.raises(Exception) as exc_info:
        client = ChatGPTClient(api_key=None)
    
    
    assert "API key must be provided for OpenAI client." in str(exc_info.value)
        
        
    monkeypatch.setenv("OPENAI_API_KEY", "test_api_key")
    client = ChatGPTClient(api_key=None, config=azure_config)
    assert client._api_key == "test_api_key"
    

        
def test_openai_api_key_passed_with_no_config(openai_config):
    '''
    Test initializing ChatGPTClient with API key passed directly and no config. The default behavior is to use OpenAI settings and an OpenAI (NOT Azure) client.
    '''
    client = ChatGPTClient(
        api_key="explicit_key"
    )

    assert client._api_key == "explicit_key"
    
    
    assert isinstance(client._config, ChatGPTClientConfig)
    assert client._config.is_azure is False
    assert client._config.api_base == "https://api.openai.com/v1/"
    assert client._config.default_model == "gpt-3.5-turbo"
    assert "gpt-3.5-turbo" in client._config.available_models



def test_openai_default_configuration_settings(openai_config):
    '''
    Test initializing ChatGPTClient with OpenAI configuration settings passed but the config is empty. Should default.
    '''
    client = ChatGPTClient(
        config=openai_config,
        api_key="key"
    )

    assert client._config.api_base == "https://api.openai.com/v1/"
    assert client._config.default_model == "gpt-3.5-turbo"
    assert "gpt-3.5-turbo" in client._config.available_models



def test_azure_api_key_from_environment(monkeypatch, azure_config):
    '''
    Test initializing ChatGPTClient with Azure configuration and API key from environment.
    '''
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "env_key")

    client = ChatGPTClient(
        config=azure_config,
        api_key=None
    )

    assert client._api_key == "env_key"


def test_azure_api_key_passed(azure_config):
    '''
    Test initializing ChatGPTClient with Azure configuration and API key passed directly.
    '''
    client = ChatGPTClient(
        config=azure_config,
        api_key="azure_key"
    )

    assert client._api_key == "azure_key"


def test_azure_configuration_settings_passed(azure_config):
    '''
    Test initializing ChatGPTClient with Azure configuration settings passed directly.
    '''
    client = ChatGPTClient(
        config=azure_config,
        api_key="key"
    )

    assert client._config.api_base == "https://example.openai.azure.com/"
    assert client._config.default_model == "gpt-4"
    assert client._config.is_azure is True



def test_azure_configuration_file_provided(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"

    config_file.write_text(yaml.safe_dump({
        "is_azure": True,
        "api_base": "https://file.azure.com/",
        "available_models": {"gpt-4": "gpt-4"},
        "default_model": "gpt-4",
        "api_version": "2024-02-01",
    }))

    config = ChatGPTClientConfig(
        is_azure=True,
        config_filepath=str(config_file),
        api_base=None,
        available_models=None,
        default_model=None,
        api_version=None,
    )

    client = ChatGPTClient(
        config=config,
        api_key="file_key"
    )

    assert client._config.api_base == "https://file.azure.com/"
    assert client._config.default_model == "gpt-4"

    # Cleanup
    config_file.unlink()
    

    
def test_missing_api_key_raises(openai_config, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    
    with pytest.raises(OpenAIAuthError):
        ChatGPTClient(config=openai_config, api_key=None)



def test_missing_config_for_azure_raises():
    with pytest.raises(AutocoderClientError) as exc_info:
        ChatGPTClient(config={
            "is_azure": True,    
        }, api_key="key")
    
        
    assert "For Azure OpenAI" in str(exc_info.value), "Configuration must be provided for Azure OpenAI client."


def test_message_history_initialization():
    
    msg_list = MessageList(messages=[])
    msg_list.add(Role.USER, "Hello")
    msg_list.add(Role.ASSISTANT, "Hi there!")

    dict_list = msg_list.to_dict()
    assert len(dict_list) == 2
    assert dict_list[0]["role"] == Role.USER.value
    assert dict_list[0]["content"] == "Hello"
    assert dict_list[1]["role"] == Role.ASSISTANT.value
    assert dict_list[1]["content"] == "Hi there!"

    client = ChatGPTClient(config=ChatGPTClientConfig(), api_key="key", message_history=msg_list)
    assert client._message_history is not None
    assert len(client._message_history.messages) == 2
    assert client._message_history.messages[0].role == Role.USER
    assert client._message_history.messages[0].content == "Hello"
    assert client._message_history.messages[1].role == Role.ASSISTANT
    assert client._message_history.messages[1].content == "Hi there!"



def test_message_history_from_file_initialization():
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"}
    ]
    
    msg_list = MessageList(messages=messages)
    dict_list = msg_list.to_dict()
    
    # Save to file and load through message_history_filepath
    config_filepath = "test_message_history.json"
    with open(config_filepath, "w") as f:
        json.dump(dict_list, f)
        
    client = ChatGPTClient(config=ChatGPTClientConfig(), api_key="key", message_history_filepath=config_filepath)
    assert client._message_history is not None
    print( client._message_history)
    assert len(client._message_history) == 2
    assert client._message_history[0]["role"] == "user"
    assert client._message_history[0]["content"] == "Hello"
    assert client._message_history[1]["role"] == "assistant"
    assert client._message_history[1]["content"] == "Hi there!"
        
    # Cleanup
    os.remove(config_filepath)
