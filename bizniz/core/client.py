from abc import ABC, abstractmethod

from typing import List, Dict, Any, Tuple

from bizniz.core.types import ResponseFormat, Message, MessageList


class BaseAIClient(ABC):

    @abstractmethod
    def get_text(
        self,
        messages,
        message_history: MessageList = None,
        message_history_filepath: str = None,
        use_message_history: bool = True,
        message_history_limit: int = 10,
        schema: dict = None,
        **kwargs,
    ) -> Tuple[str, str, List]:

        pass


    @abstractmethod
    def set_model(self, model_name: str) -> None:
        pass

    @property
    @abstractmethod
    def ai_agent(self) -> Any:
        pass
