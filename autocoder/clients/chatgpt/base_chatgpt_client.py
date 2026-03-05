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

from autocoder.clients.chatgpt.messages import Message, MessageList
from autocoder.clients.base_ai_client import BaseAIClient

from .errors import (
    OpenAIClientError,
    OpenAIRateLimit,
    OpenAIAuthError,
    OpenAIInvalidRequest
)

from autocoder.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig


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

        if api_key is None:
            raise OpenAIAuthError("API key must be provided.")

        self._api_key = api_key
        self._config = config
        self._model_name = model_name or config.default_model

        self.max_tokens = max_tokens
        self._db_client = db_client

        self.on_message_callback = on_message_callback
        self.on_message_history_update_callback = on_message_history_update_callback

        if self._db_client:
            self._create_db_table_if_not_exists()

        self._set_message_history(message_history, message_history_filepath)

        # NOTE:
        # Subclasses MUST initialize self._ai_agent
        self._ai_agent = None

    # ------------------------------------------------------------------
    # MESSAGE HISTORY
    # ------------------------------------------------------------------

    def _set_message_history(
        self,
        message_history: Optional[List[Message]],
        message_history_filepath: Optional[str] = None
    ):
        self._message_history = []
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
        instruction_messages,
        messages,
        max_tokens=None,
        response_format=None,
        job_description=None,
        use_message_history=True,
        message_history_limit=10
    ):

        try:

            instruction_messages = instruction_messages.to_dict()
            instruction_messages = [m for m in instruction_messages if m["role"] == "system"]

        except AttributeError:
            pass

        try:
            messages = messages.to_dict()
        except AttributeError:
            pass

        if use_message_history:

            history = self._message_history[-message_history_limit:]

            message_with_history = instruction_messages + history + messages

        else:

            message_with_history = instruction_messages + messages

        content = None
        try:

            content = self._create_completion(
                message_with_history,
                max_tokens or self.max_tokens,
                response_format
            )

            

            filtered = [m for m in messages if m["role"] != "system"]

            self._message_history.extend(filtered)

            self._message_history.append({
                "role": "assistant",
                "content": content
            })

            self._save_message_history_to_file()

            return content, str(uuid.uuid4())

        except AuthenticationError as e:
            raise OpenAIAuthError(str(e))

        except RateLimitError as e:
            raise OpenAIRateLimit(str(e))

        except BadRequestError as e:
            raise OpenAIInvalidRequest(str(e))

        except Exception as e:
            raise OpenAIClientError(str(e))