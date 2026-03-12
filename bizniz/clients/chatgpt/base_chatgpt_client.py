"""
BaseChatGPTClient

Contains all shared logic between OpenAIChatGPTClient and AzureChatGPTClient.

Provider-specific code should ONLY exist in subclasses.
"""

import os
import json
import uuid
import yaml
from pathlib import Path

from typing import Optional, Any, Union, List, Dict, Tuple, Callable

from openai import BadRequestError, AuthenticationError, RateLimitError

from bizniz.clients.chatgpt.messages import Message, MessageList, normalize_messages
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.types.response_format import ResponseFormat

from .errors import (
    OpenAIClientError,
    OpenAIRateLimit,
    OpenAIInsufficientFunds,
    OpenAIAuthError,
    OpenAIInvalidRequest
)

from bizniz.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig
from bizniz.clients.chatgpt.chatgpt_client_errors import AutocoderClientError


class BaseChatGPTClient(BaseAIClient):
    """
    Shared implementation for OpenAIChatGPTClient and AzureChatGPTClient.

    IMPORTANT:
    This class contains NO provider-specific SDK calls.
    """

    def __init__(
        self,
        config: ChatGPTClientConfig,
        api_key: str,
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
        self._ai_agent = None  # Subclasses MUST set this to the appropriate SDK client instance.
        # END SET THE AI AGENT //////////////////////////////////////////////////////////////////////////////////
        
        self._set_message_history(message_history, message_history_filepath)
        

    # ------------------------------------------------------------------
    # MESSAGE HISTORY
    # ------------------------------------------------------------------

    def _set_message_history(
        self,
        message_history: Optional[List[Message]],
        message_history_filepath: Optional[str] = None
    ):
        self._message_history: List[Dict[str, Any]] = []
        self._message_history_filepath = message_history_filepath

        if message_history and message_history_filepath:
            raise ValueError("Cannot provide both message_history and message_history_filepath")

        if message_history_filepath and os.path.exists(message_history_filepath):

            try:
                with open(message_history_filepath) as f:
                    self._message_history = json.load(f)
            except Exception:
                self._message_history = []

        elif message_history:

            if isinstance(message_history, MessageList):
                self._message_history = message_history.to_dict()

            elif all(isinstance(m, dict) for m in message_history):
                self._message_history = message_history

            else:
                raise ValueError("Invalid message history format")

        else:
            self._message_history = []



    def _save_message_history_to_file(self):

        if not self._message_history_filepath:
            return

        try:
            with open(self._message_history_filepath, "w") as f:
                json.dump(self._message_history, f)

        except Exception:
            pass

    # ------------------------------------------------------------------
    # DATABASE LOGGING
    # ------------------------------------------------------------------

    def _create_db_table_if_not_exists(self):

        query = """
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
            self._db_client.execute(query)
        except Exception:
            pass

    def _log_request(
        self,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        job_description: str = None,
        data: Any = None
    ):

        if not self._db_client:
            return str(uuid.uuid4())

        job_id = str(uuid.uuid4())

        query = """
        INSERT INTO OpenAI_Request
        (model,data,input_tokens,output_tokens,job_id,job_description)
        VALUES (%s,%s,%s,%s,%s,%s)
        """

        try:

            self._db_client.insert(
                query,
                (model_name, str(data), input_tokens, output_tokens, job_id, job_description)
            )

        except Exception:
            pass

        return job_id

    # ------------------------------------------------------------------
    # MODEL SWITCHING
    # ------------------------------------------------------------------

    def set_model(self, model_name: str):
        """Switch the model used for subsequent API calls."""
        self._model_name = model_name

    # ------------------------------------------------------------------
    # ABSTRACT PROVIDER CALL
    # ------------------------------------------------------------------

    def _create_completion(self, messages, max_tokens, response_format):
        """
        Must be implemented by subclasses.

        Responsible for making the provider API call.
        """
        raise NotImplementedError()

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------
    def get_text(
        self,
        messages: Union[MessageList, List[dict]],
        max_tokens: Optional[int] = None,
        response_format: ResponseFormat = ResponseFormat.TEXT,
        job_description: Optional[str] = None,
        message_history: Optional[MessageList] = None,
        message_history_filepath: Optional[str] = None,
        use_message_history: bool = False,
        message_history_limit: Optional[int] = 10,
        temperature: float = 0.0,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, MessageList]:

        # Initialize history if provided
        if message_history is not None or message_history_filepath is not None:
            self._set_message_history(message_history, message_history_filepath)

        # Normalize input messages
        try:
            messages = self._normalize_messages(messages)
        except Exception as e:
            raise AutocoderClientError(f"Failed to process messages: {e}")

        # Build message list with optional history
        if use_message_history and self._message_history:

            history = (
                self._message_history[-message_history_limit:]
                if message_history_limit
                else list(self._message_history)
            )

            message_with_history = history + list(messages)

        else:
            message_with_history = list(messages)

        try:

            _response_format = response_format.value if hasattr(response_format, "value") else response_format

            output_messages: MessageList = self._create_completion(
                messages=message_with_history,
                max_tokens=max_tokens or self.max_tokens or 10_000,
                response_format=_response_format,
                schema=schema,
                temperature=temperature,
            )

            # Log request
            if self._db_client:
                job_id = self._log_request(
                    model_name=self._model_name,
                    input_tokens=output_messages.input_tokens,
                    output_tokens=output_messages.output_tokens,
                    job_description=job_description or "ChatGPT text completion",
                    data=message_with_history
                )
            else:
                job_id = str(uuid.uuid4())

            # ---------------------------------------
            # Update message history
            # ---------------------------------------

            if use_message_history:

                # Store assistant responses
                for m in output_messages:
                    if m.role != "system":
                        self._message_history.append({
                            "role": m.role,
                            "content": m.content
                        })

                # Trim history if needed
                if message_history_limit and len(self._message_history) > message_history_limit * 2:
                    self._message_history = self._message_history[-message_history_limit * 2:]

            # ---------------------------------------
            # Callbacks
            # ---------------------------------------

            if self.on_message_callback:
                for message in output_messages:
                    self.on_message_callback(
                        Message(role=message.role, content=message.content)
                    )

            if self.on_message_history_update_callback:
                self.on_message_history_update_callback(self._message_history)

            # Persist history
            self._save_message_history_to_file()

            # Combine assistant messages
            content = "".join(
                m.content for m in output_messages if m.role == "assistant"
            )

            return content, job_id, output_messages

        except AuthenticationError as e:
            raise OpenAIAuthError(str(e))

        except RateLimitError as e:
            error_msg = str(e).lower()
            if any(phrase in error_msg for phrase in [
                "insufficient_quota", "exceeded your current quota",
                "billing_hard_limit_reached", "billing hard limit",
                "you have insufficient funds", "account is not active",
            ]):
                raise OpenAIInsufficientFunds(str(e))
            raise OpenAIRateLimit(str(e))

        except BadRequestError as e:
            from bizniz.clients.chatgpt.errors import OpenAIContextLengthExceeded
            error_msg = str(e).lower()
            if "context_length_exceeded" in error_msg or "context window" in error_msg:
                raise OpenAIContextLengthExceeded(str(e))
            raise OpenAIInvalidRequest(str(e))

        except Exception as e:
            raise OpenAIClientError(f"Unknown error: {e}")


            
            
        
    def _normalize_messages(self, messages):
        return normalize_messages(messages)