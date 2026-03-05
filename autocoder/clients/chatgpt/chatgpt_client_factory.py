"""
ChatGPTClientFactory

Creates the correct ChatGPT client based on configuration.
"""

from typing import Optional
from pydantic import BaseModel
from typing import Dict

from autocoder.clients.chatgpt.azure_chatgpt_client import AzureChatGPTClient
from autocoder.clients.chatgpt.openai_chatgpt_client import OpenAIChatGPTClient
from autocoder.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig

class AutocoderClientError(Exception):
    pass


class ChatGPTClientFactory:

    @staticmethod
    def create_client(
        config: ChatGPTClientConfig,
        api_key: str,
        **kwargs
    ):

        # NOTE
        # Future extension point if more providers appear

        if config.is_azure:

            return AzureChatGPTClient(
                config=config,
                api_key=api_key,
                **kwargs
            )

        return OpenAIChatGPTClient(
            config=config,
            api_key=api_key,
            **kwargs
        )