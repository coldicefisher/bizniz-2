from abc import ABC, abstractmethod
import os
import yaml
import json
import uuid

from typing import List, Dict, Any, Tuple


from autocoder.clients.chatgpt.types.response_format import ResponseFormat
# from python_core.data_connectors.mysql_connector import MySQLConnector

from autocoder.clients.chatgpt.messages import Message, MessageList



class BaseAIClient(ABC):

    @abstractmethod
    def get_text(
        self,
        instruction_messages,
        messages,
        message_history: MessageList = None,
        message_history_filepath: str = None,
        use_message_history: bool = True,
        message_history_limit: int = 10
    ) -> Tuple[str, str]:

        pass


    @property
    @abstractmethod
    def ai_agent(self) -> Any:
        pass