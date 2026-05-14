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

    def try_create_cache(self, messages) -> Any:
        """Attempt to create a provider-side cache for ``messages``
        (typically the system prompt + initial user message of a
        tool-loop run).

        Returns an opaque cache identifier (string) on success, or
        ``None`` if the provider doesn't support caching, the content
        is too small to be cacheable, or any error occurs. Callers
        pass the identifier back via ``cached_content_name`` on
        subsequent ``get_text`` calls.

        Default: no-op (returns None) — only the Gemini client
        implements this today.
        """
        return None
