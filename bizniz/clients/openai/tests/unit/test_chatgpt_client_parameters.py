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



def test_chatgpt_client_openai_parameters(openai_config, azure_config):
    """Test ChatGPTClientConfig parameters."""
    # Test OpenAI configuration
    openai_client = ChatGPTClient(config=openai_config, api_key="test_api_key")
    print(f"OpenAI Client Config: {openai_client.config}")
    assert openai_client.config.is_azure is False
    assert openai_client.config.api_base == "https://api.openai.com/v1/"
    assert len(openai_client.config.available_models) > 0
    assert openai_client.config.default_model == "gpt-3.5-turbo"
    
        

def test_chatgpt_client_azure_parameters(openai_config, azure_config):
    # Test Azure configuration
    azure_client = ChatGPTClient(config=azure_config, api_key="test_api_key")
    assert azure_client.config.is_azure is True
    assert azure_client.config.api_base == "https://example.openai.azure.com/"
    assert azure_client.config.available_models == {"gpt-4": "gpt-4"}
    assert azure_client.config.default_model == "gpt-4"
    
    
    
