"""
AzureChatGPTClient

Handles Azure OpenAI API differences.
"""

from openai import AzureOpenAI

from autocoder.clients.chatgpt.base_chatgpt_client import BaseChatGPTClient


class AzureChatGPTClient(BaseChatGPTClient):

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        self._ai_agent = AzureOpenAI(
            api_key=self._api_key,
            api_version=self._config.api_version,
            azure_endpoint=self._config.api_base
        )




    @property
    def ai_agent(self):
        return self._ai_agent


    # Azure requires chat.completions

    def _create_completion(self, messages, max_tokens, response_format):

        response = self._ai_agent.chat.completions.create(
            model=self._model_name,
            messages=messages,
            max_completion_tokens=max_tokens
        )
        
        content = response.choices[0].message.content
        return content