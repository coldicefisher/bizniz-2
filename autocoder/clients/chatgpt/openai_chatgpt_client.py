"""
OpenAIChatGPTClient

Handles the standard OpenAI API.
"""

from openai import OpenAI

from .base_chatgpt_client import BaseChatGPTClient


class OpenAIChatGPTClient(BaseChatGPTClient):

    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs)

        self._ai_agent = OpenAI(api_key=self._api_key)



    @property
    def ai_agent(self):
        return self._ai_agent
    
    
    # NOTE
    # Provider specific call implemented here

    def _create_completion(self, messages, max_tokens, response_format):
        print(self._model_name)
        raise NotImplementedError("This method should be overridden by subclasses to implement provider-specific API calls.")
    
        response = self._ai_agent.responses.create(
            model=self._model_name,
            input=messages,
            max_output_tokens=max_tokens
        )
        
        return response.output.text