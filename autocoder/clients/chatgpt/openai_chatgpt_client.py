"""
OpenAIChatGPTClient

Handles the standard OpenAI API.
"""

from openai import OpenAI

from autocoder.clients.chatgpt.base_chatgpt_client import BaseChatGPTClient

from openai.types.responses.response import Response as OpenAIResponse
from openai.types.responses import ResponseOutputItem, ResponseOutputMessage

from typing import Optional, List, Dict

from autocoder.clients.chatgpt.messages import Message, MessageList


class OpenAIChatGPTClient(BaseChatGPTClient):

    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs)

        self._ai_agent = OpenAI(api_key=self._api_key)



    @property
    def ai_agent(self) -> OpenAI:
        return self._ai_agent
    
    
    # NOTE
    # Provider specific call implemented here

    def _create_completion(self, messages, max_tokens, response_format) -> List[Message]:
        # Normalize messages into dict format for the OpenAI SDK
        if isinstance(messages, MessageList):
            messages = messages.to_dict()

        elif isinstance(messages, list):
            normalized = []
            for m in messages:
                if isinstance(m, Message):
                    normalized.append(m.to_dict())
                else:
                    normalized.append(m)
            messages = normalized
        
        # if isinstance(messages, MessageList):
        #     messages = messages.to_dict()

        # elif isinstance(messages, list) and isinstance(messages[0], Message):
        #     messages = [m.to_dict() for m in messages]

        if self._model_name == 'gpt-3.5-turbo':        
            response: OpenAIResponse = self._ai_agent.responses.create(
                model=self._model_name,
                input=messages,
                max_output_tokens=max_tokens
            )
                    
            
            output_messages: List[ResponseOutputMessage] = response.output
            print(f"Raw response from OpenAI API: ")
            print(response)
            print(type(response))
            print(f"Parsed messages: ")        
            print(output_messages)
            print(type(output_messages))
            out = []
            
            for m in output_messages:
                for content in m.content:
                    print(f"Content from message: {content}")
                    print(f"Message from model: {m}")
                    out.append(Message(
                        role=m.role,
                        content=content.text
                    ))
            
            print("Out: ")
            print(out)
            message_list: MessageList = MessageList(messages=out, input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens)
            print(f"Constructed MessageList: ")
            print(str(message_list))
            return message_list
        
        else:
            raise NotImplementedError(f"Model {self._model_name} is not supported in OpenAIChatGPTClient.")