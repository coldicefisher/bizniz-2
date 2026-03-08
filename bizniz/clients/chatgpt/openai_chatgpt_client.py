"""
OpenAIChatGPTClient

Handles the standard OpenAI API.
"""

from openai import OpenAI

from bizniz.clients.chatgpt.base_chatgpt_client import BaseChatGPTClient

from openai.types.responses.response import Response as OpenAIResponse
from openai.types.responses import ResponseOutputItem, ResponseOutputMessage
from openai.types.chat.chat_completion import ChatCompletion, Choice as ChatChoice

from typing import Optional, List, Dict, Any

from bizniz.clients.chatgpt.messages import Message, MessageList
from bizniz.clients.chatgpt.types.response_format import ResponseFormat, parse_response_format


class OpenAIChat3GPTClient(BaseChatGPTClient):

    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs)

        self._ai_agent = OpenAI(api_key=self._api_key)



    @property
    def ai_agent(self) -> OpenAI:
        return self._ai_agent
    
    
    # NOTE
    # Provider specific call implemented here

    def _create_completion(self, 
                           messages, 
                           max_tokens, 
                           response_format: ResponseFormat, 
                           schema: Optional[Dict[str, Any]]=None,
                           temperature: Optional[float] = 0.0
    ) -> List[Message]:
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
        _response_format = parse_response_format(response_format, schema)
        
        if _response_format["type"] == "json_object":
            pass  # No special handling needed for JSON_OBJECT
        elif _response_format["type"] == "json_schema":
            _response_format["type"] = "json_object"
            try:
                del _response_format["json_schema"]
            except KeyError:
                pass
            
        else:
            raise NotImplementedError(f"Response format {_response_format['type']} is not supported in OpenAIChatGPTClient.")
        
        
        if self._model_name == 'gpt-3.5-turbo':        
            if isinstance(max_tokens, int):
                max_tokens = max_tokens
            else:
                max_tokens = 2048
                
            if max_tokens > 4096:
                max_tokens = 4096
                
            response: ChatCompletion = self._ai_agent.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=temperature if temperature is not None else 0.0,
                max_tokens=max_tokens if max_tokens is not None else 4096,
                response_format=_response_format
            )
                    
            
            output_messages: List[ChatChoice] = response.choices
            out = []
            
            for m in output_messages:
                content = m.message.content
                out.append(Message(
                    role=m.message.role,
                    content=content
                ))
        
            message_list: MessageList = MessageList(messages=out, input_tokens=response.usage.prompt_tokens, output_tokens=response.usage.completion_tokens)
            
            return message_list
        
        else:
            raise NotImplementedError(f"Model {self._model_name} is not supported in OpenAIChatGPT3Client.")
        
        

        
class OpenAIChat4GPTClient(BaseChatGPTClient):

    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs)

        self._ai_agent = OpenAI(api_key=self._api_key)



    @property
    def ai_agent(self) -> OpenAI:
        return self._ai_agent
    
    
    # NOTE
    # Provider specific call implemented here

    def _create_completion(self, 
                           messages, 
                           max_tokens, 
                           response_format: ResponseFormat, 
                           schema: Optional[Dict[str, Any]] = None,
                           temperature: Optional[float] = 0.0   
    ) -> List[Message]:
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

        match self._model_name:
            case 'gpt-4o-mini':
                
                kwargs = {
                    "model": "gpt-4o-mini",
                    "temperature": temperature if temperature is not None else 0.0,
                    "max_output_tokens": max_tokens if max_tokens is not None else 4096,
                }
                
                    
                if response_format == 'json_schema':
                    if schema is None:
                        raise ValueError("Schema must be provided for JSON_SCHEMA response format.")
                    
                    
                    kwargs['text'] = {
                    "format": {
                        "type": "json_schema",
                        "name": "generate_code",
                        "schema": schema,
                        "strict": True
                    }
                }
                else:
                    raise NotImplementedError(f"Response format {response_format} is not supported in OpenAIChat4GPTClient.")
                
                if response_format != 'json_schema':
                    raise NotImplementedError(f"Response format {response_format} is not supported in OpenAIChat4GPTClient.")
                
                kwargs["input"] = messages
                # RESPONSES API IMPLEMENTATION -- CANNOT USE RESPONSE FORMAT.
                response: OpenAIResponse = self._ai_agent.responses.create(**kwargs)
                        
                
                output_messages: List[ResponseOutputMessage] = response.output
                out = []
                
                for m in output_messages:
                    for content in m.content:
                        out.append(Message(
                            role=m.role,
                            content=content.text
                        ))
                
                message_list: MessageList = MessageList(messages=out, input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens)
                return message_list
            

            
            case _:
                raise NotImplementedError(f"Model {self._model_name} is not supported in OpenAIChatGPTClient.")