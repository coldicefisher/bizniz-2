import os
import yaml
import uuid
import json
from pathlib import Path

from typing import Optional, Any, Union, List, Dict, Tuple, Callable

from openai import OpenAI, BadRequestError, AuthenticationError, RateLimitError
from openai import AzureOpenAI, OpenAI

from .models import OpenAIModel
from .errors import (
    OpenAIClientError, OpenAIRateLimit, OpenAIAuthError, OpenAIInvalidRequest
)
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
# from python_core.data_connectors.mysql_connector import MySQLConnector

from typing import List, Dict, Any, Tuple, Union, Optional, Callable
from pydantic import BaseModel
import importlib.util
from pathlib import Path


from .messages import Message, MessageList

from bizniz.clients.base_ai_client import BaseAIClient


class ChatGPTClientConfig(BaseModel):
    '''
    config_filepath can be used in leiu of passing the other configuration parameters 
    directly. If config_filepath is provided, the client will attempt to load the 
    configuration from the specified file path. The file should be in YAML format and contain the 
    necessary configuration fields (is_azure, api_base, available_models, default_model). If 
    both config_filepath and direct parameters are provided, the client will prioritize loading
    from the config_filepath.
    '''
    is_azure: Optional[bool] = False
    api_base: Optional[str] = None
    api_version: Optional[str] = None
    available_models: Optional[Dict[str, str]] = None
    default_model: Optional[str] = None
    
    config_filepath: Optional[str] = None
    

class AutocoderClientError(Exception):
    """Base exception for Autocoder client errors."""
    pass



class ChatGPTClient(BaseAIClient):
    """
    High-level typed client for ChatGPT.
    """

    def __init__(self, 
                    config: ChatGPTClientConfig = None,
                    api_key: str=None, 
                    model_name: str = None,
                    max_tokens: int = 10_000, 
                 
                    db_client: Any = None,
                    message_history: Optional[List[MessageList]] = None,
                    message_history_filepath: Optional[str] = None,
                    on_message_callback: Optional[Callable[[Message], None]] = None,
                    on_message_history_update_callback: Optional[Callable[[List[Message]], None]] = None
                 
    ):
        
        
        self._db_client = None
        if db_client is not None:
            self._db_client = db_client
            
        # We want to create the database table if we have a db client.
        if self._db_client is not None:
            self._create_db_table_if_not_exists()
            
        
        self.max_tokens = max_tokens
        self.on_message_callback = on_message_callback
        self.on_message_history_update_callback = on_message_history_update_callback
            
        # SET API KEY ////////////////////////////////////////////////////////////////////////////////////////
        if api_key is None:
            api_key = os.environ.get("OPENAI_API_KEY", None)
        
        if api_key is None:
            raise OpenAIAuthError("API key must be provided for OpenAI client.")
        
        self._api_key = api_key
        # END SET API KEY //////////////////////////////////////////////////////////////////////////////////////
        
        
        # SET THE CONFIGURATION ////////////////////////////////////////////////////////////////////////////////
        self._config: ChatGPTClientConfig = None
        
        if config is not None:
            if isinstance(config, dict):
                self._config = ChatGPTClientConfig(**config)
            else:
                self._config = config
            
        elif config is None and self._api_key is None:
            raise AutocoderClientError("Configuration must be provided for ChatGPT client if no API Key is provided.")
        
        elif config is None and self._api_key is not None:
            self._config = ChatGPTClientConfig()
        
        
        # We either need the config file path or the config object to be provided.
        if self._config.config_filepath is not None:
            try:
                # Make it relative to the project root if it's not an absolute path.
                config_path = Path(self._config.config_filepath)

                if not config_path.is_absolute():
                    config_path = Path.cwd() / config_path

                with open(config_path, "r") as f:
                    file_config = yaml.safe_load(f)
                    self._config = ChatGPTClientConfig(**file_config)
                    
            except Exception as e:
                raise AutocoderClientError(f"Failed to load configuration from file: {e}")
            
        
        # Fail if finally we do not have a configuration object or if required fields are missing.
        if self._config.is_azure is None and (self._config.api_base is None or self._config.available_models is None or self._config.default_model is None):
            raise AutocoderClientError("Configuration must include api_base, available_models, and default_model.")
        
        
        # Azure specific settings
        if self._config.is_azure:
            if self._config.api_base is None or self._config.available_models is None or self._config.default_model is None:
                raise AutocoderClientError("For Azure OpenAI, api_base and available_models must be set in the configuration.")            
    
        else:
            if self._config.api_base is None:
                self._config.api_base = "https://api.openai.com/v1/"
            if self._config.available_models is None:
                self._config.available_models = {
                    "gpt-4": "gpt-4",
                    "gpt-3.5-turbo": "gpt-3.5-turbo"
                }
            if self._config.default_model is None:
                self._config.default_model = "gpt-3.5-turbo"
        
        # Set the API version from the available_models if not explicitly set (for Azure).
        self._model_name = model_name or self._config.default_model
        self._api_version = self._config.available_models.get(self._model_name, None) if self._config.is_azure else None
        # END SET THE CONFIGURATION ////////////////////////////////////////////////////////////////////////////
        
        
        # SET THE AI AGENT /////////////////////////////////////////////////////////////////////////////////////
        if self._config.is_azure:
            self._ai_agent = AzureOpenAI(
                api_key=self._api_key,
                api_version=self._api_version,
                azure_endpoint=self._config.api_base,
            )
        else:
            self._ai_agent = OpenAI(
                api_key=self._api_key,
            )
        # END SET THE AI AGENT //////////////////////////////////////////////////////////////////////////////////
        
        self._set_message_history(message_history, message_history_filepath)
        
        
        
    def _set_message_history(self, message_history: Optional[List[Message]], message_history_filepath: Optional[str] = None):
        # SET MESSAGE HISTORY ///////////////////////////////////////////////////////////////////////////////////
        self._message_history = []
        self._message_history_filepath = message_history_filepath
        
        if message_history and message_history_filepath:
            raise ValueError("Cannot provide both message_history and message_history_filepath. Please choose one.")
        
        
        # Load message history from file if provided.
        if message_history_filepath and os.path.exists(message_history_filepath):
            with open(message_history_filepath, "r") as f:
                try:
                    message_history_data = json.load(f)
            
                    self._message_history = message_history_data
                except Exception as e:
                    
                    self._message_history = []
        
        
        # Load message history from a passed object.
        elif message_history is not None:
            # Validate message history before using it.
        
            if isinstance(message_history, MessageList):
                self._message_history = message_history.to_dict()
            elif all(isinstance(m, dict) for m in message_history):
                self._message_history = message_history
            else:
                raise ValueError("message_history must be a list of MessageList or dict objects.")
            
            self._message_history = message_history 
        
        # Message history file or passed object not provided.       
        else:
            self._message_history = []

        # END SET MESSAGE HISTORY /////////////////////////////////////////////////////////////////////////////

        
        
    def _create_db_table_if_not_exists(self):
        if not self._db_client:
            raise OpenAIClientError("Database client is not initialized for logging.")
        
        create_table_query = """
            CREATE TABLE IF NOT EXISTS OpenAI_Request (
                id INT AUTO_INCREMENT PRIMARY KEY,
                model VARCHAR(255),
                data TEXT,
                input_tokens INT,
                output_tokens INT,
                job_id VARCHAR(255),
                job_description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        try:
            self._db_client.execute(create_table_query)
        except Exception as e:
            print(f"Failed to create database table: {e}")
            
            
        
    def _save_message_history_to_file(self):
        if self._message_history_filepath:
            try:
                with open(self._message_history_filepath, "w") as f:
                    json.dump(self._message_history, f)
            except Exception as e:
                print(f"Failed to save message history to file: {e}")
                
                
    def __destroy__(self):
        if hasattr(self, '_db_client') and self._db_client is not None:
            try:
                self._db_client.close()
            except Exception as e:
                pass
            
        # Save the history to a file for debugging purposes.
        self._save_message_history_to_file()


    @property
    def ai_agent(self) -> Union[AzureOpenAI, OpenAI]:
        return self._ai_agent
    
    
    @property
    def config(self) -> ChatGPTClientConfig:
        return self._config
                    
    # @property
    # def db_client(self):
    #     if not hasattr(self, '_db_client') or self._db_client is None:
    #         self._db_client = self.get_production_conn()
            
    #     return self._db_client
    
    
    
    # TEXT COMPLETION ///////////////////////////////////////////////////////////////////////////////
    def get_text(
        self,
        
        instruction_messages: Union[MessageList, List[dict]],
        messages: Union[MessageList, List[dict]],
        
        max_tokens: Optional[int] = None,
        
        response_format: ResponseFormat = ResponseFormat.TEXT,
        
        job_description: str = None,
        
        message_history: Optional[MessageList] = None,
        message_history_filepath: Optional[str] = None,
        use_message_history: Optional[bool] = True,
        message_history_limit: Optional[int] = 10,
    ) -> Tuple[str, str]:
        """
        Generate a text completion using the configured OpenAI/Azure client.
        Args:
            instruction_messages: System-level instruction messages (MessageList or list of dicts).
            messages: User/assistant messages to complete (MessageList or list of dicts).
            max_tokens: Optional maximum output tokens.
            response_format: Desired response format (e.g., text or JSON).
            job_description: Optional description for logging.
            use_message_history: Whether to prepend stored message history.
            message_history_limit: Maximum history messages to include.
        Returns:
            Tuple of (completion text, job_id).
        Raises:
            AutocoderClientError: On message preprocessing failures.
            OpenAIAuthError: If authentication fails.
            OpenAIRateLimit: If rate-limited.
            OpenAIInvalidRequest: If the request is invalid.
            OpenAIClientError: For other unexpected errors.
        """
        
        if message_history is not None or message_history_filepath is not None:
            self._set_message_history(message_history, message_history_filepath)
        
        try:
            instruction_messages = instruction_messages.to_dict()
            # Filter out any messages that are not system messages
            instruction_messages = [m for m in instruction_messages if m["role"] == "system"]
        except AttributeError:
            pass  # Assume it's already a list of dicts
        except Exception as e:
            raise AutocoderClientError(f"Failed to process instruction messages: {e}")
        
        try:
            messages = messages.to_dict()
        except AttributeError:
            pass  # Assume it's already a list of dicts
        except Exception as e:
            raise AutocoderClientError(f"Failed to process messages: {e}")
        
        message_with_history = None
        # If we are using message history, we want to limit the number of messages we include in the prompt to avoid hitting context length limits. We will include the most recent messages up to the limit.
        if use_message_history:
            _message_history = self._message_history[-message_history_limit:] if message_history_limit else self._message_history
            message_with_history = instruction_messages + _message_history + messages
        else:
            message_with_history = instruction_messages + messages
            
        
        # if model_name == 'o4-mini':
        try:
            # Azure has to use chat.completions whereas OpenAI can use responses API. The response format parameter is not currently supported in the Azure SDK for Python, so we will ignore it for Azure.
            completion = None
            _response_format = response_format
            # If the response format is an enum, get the value
            if hasattr(_response_format, "value"):
                _response_format = _response_format.value
                
            if self._config.is_azure:
                completion = self._ai_agent.chat.completions.create(
                    model=self._model_name,
                    messages=message_with_history,
                    max_completion_tokens=max_tokens or self.max_tokens or 10_000,
                    response_format=_response_format,
                )
            
            else:
                completion = self._ai_agent.responses.create(
                    model=self._model_name,
                    input=message_with_history,
                    max_output_tokens=max_tokens or self.max_tokens or 10_000,
                    response_format=_response_format, # Not supported for our Azure SDK in Responses API.
                )
            
            
            job_id = None
            
            if self._db_client:
                job_id = self._log_request(
                    model_name=self._model_name,
                    input_tokens=completion.usage.prompt_tokens,
                    output_tokens=completion.usage.completion_tokens,
                    job_description=job_description or "ChatGPT text completion",
                    data=message_with_history
                )
            else:
                job_id = str(uuid.uuid4())
                
            
            # Add the messages to history. Filter out any system messages since those are not relevant to the history and can cause issues with context length.
            filtered_messages = [m for m in messages if m["role"] != "system"]
            self._message_history.extend(filtered_messages)
            # Append the assistant's response to the message history as well.
            self._message_history.append({
                "role": "assistant",
                "content": completion.choices[0].message.content
            })
            
            # If we have a callback, call it for each message in the response.
            if self.on_message_callback:
                for choice in completion.choices:
                    self.on_message_callback(Message(
                        role=choice.message.role, 
                        content=choice.message.content
                    ))
                    
            # If we have a message history update callback, call it with the updated message history.
            if self.on_message_history_update_callback:
                self.on_message_history_update_callback(self._message_history)
                    
            # Save the message history to file if applicable.
            self._save_message_history_to_file()
            return completion.choices[0].message.content, job_id
        

        except AuthenticationError as e:
            raise OpenAIAuthError(str(e))
        except RateLimitError as e:
            raise OpenAIRateLimit(str(e))
        except BadRequestError as e:
            raise OpenAIInvalidRequest(str(e))
        except Exception as e:
            raise OpenAIClientError(f"Unknown error: {e}")
        
    # else:
    #     raise NotImplementedError(f"Text completion is not implemented for model {model_name}")




    # VISION ///////////////////////////////////////////////////////////////////////////////
    def vision(
        self,
        messages: MessageList,
        model: OpenAIModel = OpenAIModel.VISION_PREVIEW,
        max_tokens: Optional[int] = None,
        job_description: str = None
    ) -> str:
        try:
            messages = messages.to_dict()
        except AttributeError:
            pass  # Assume it's already a list of dicts
        
        try:
            
            completion = self.chat.completions.create(
                model=model.value,
                messages=messages.to_dict(),
                max_tokens=max_tokens or self.max_tokens,
            )

            job_id = self._log_request(
                model_name=model.value,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                job_description=job_description or "ChatGPT vision completion",
                data=messages
            )

            return completion.choices[0].message.content, job_id

        except Exception as e:
            raise OpenAIClientError(str(e))




    # @classmethod
    # def get_production_conn(cls) -> MySQLConnector:
    #     return MySQLConnector(db_config={
    #         "host": "production-mysql",
    #         "user": "www",
    #         "password": "Z1nraM",
    #         "database": "Production",
    #         "port": 3306
    #     })




    def _log_request(self, model_name: str, input_tokens: int, output_tokens: int, job_description: str=None, data: Any=None):
        """
        Logs request data to the database.
        """
        if not self._db_client:
            raise OpenAIClientError("Database client is not initialized for logging.")
        job_id = str(uuid.uuid4())
        try:
            query = """
                INSERT INTO OpenAI_Request (model, data, input_tokens, output_tokens, job_id, job_description)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            self._db_client.insert(
                query,
                (model_name, str(data), input_tokens, output_tokens, job_id, job_description)
            )
        except Exception as e:
            pass
            # print(f"Failed to log request data: {e}")


        return job_id


    def get_cost_for_job_description(self, job_description: str, input_token_cost: float, output_token_cost: float) -> float:
        """
        Utility to get cost of a previous job by description.
        """
        if not self._db_client:
            raise OpenAIClientError("Database client is not initialized for logging.")
        
        query = """
            SELECT SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens
            FROM OpenAI_Request
            WHERE job_description = %s
        """
        result = self.db_client.fetch_one(query, (job_description,))
        if result:
            return (
                float(result["input_tokens"]) * (input_token_cost / 1_000_000) +
                float(result["output_tokens"]) * (output_token_cost / 1_000_000)
            )
            
        return 0.0
    
    
    

    @property
    def message_history(self) -> List[Dict[str, Any]]:
        return self._message_history