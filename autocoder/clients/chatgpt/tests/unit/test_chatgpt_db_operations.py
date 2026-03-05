import pytest
from unittest.mock import MagicMock


from autocoder.clients.chatgpt.openai_chatgpt_client import ChatGPTClient, ChatGPTClientConfig, AutocoderClientError
from autocoder.clients.chatgpt.messages import Message, MessageList
from autocoder.clients.chatgpt.types.roles import Role
from autocoder.clients.chatgpt.errors import OpenAIAuthError

from openai import OpenAI, AzureOpenAI




def test_logs_request(openai_config, mock_completion):
    mock_db = MagicMock()
    
    client = ChatGPTClient(
        config=openai_config,
        api_key="test",
        db_client=mock_db
    )
    
    client._ai_agent = MagicMock()
    client._ai_agent.responses.create.return_value = mock_completion
    
    client.get_text(
        instruction_messages=[{"role": "system", "content": "x"}],
        messages=[{"role": "user", "content": "y"}]
    )
    
    mock_db.insert.assert_called_once()
    