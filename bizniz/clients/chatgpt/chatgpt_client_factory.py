"""
ChatGPTClientFactory

Creates the correct ChatGPT client based on configuration.
"""

from typing import Optional
from pydantic import BaseModel
from typing import Dict

from bizniz.clients.chatgpt.azure_chatgpt_client import AzureChatGPTClient
from bizniz.clients.chatgpt.openai_chatgpt_client import OpenAIChat3GPTClient, OpenAIChat4GPTClient
from bizniz.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig



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


        match config.default_model:
            case 'gpt-3.5-turbo':
                return OpenAIChat3GPTClient(
                    config=config,
                    api_key=api_key,
                    **kwargs
                )

            case 'gpt-4o-mini' | 'gpt-4o' | 'o3-mini' | 'gpt-5':
                return OpenAIChat4GPTClient(
                    config=config,
                    api_key=api_key,
                    **kwargs
                )

            case _:
                raise NotImplementedError(f"Model {config.default_model} is not supported in ChatGPTClientFactory.")