# Backward-compatibility shim — implementation moved to bizniz.core.types
from bizniz.core.types import Message, MessageList, normalize_messages, Role, ResponseFormat

__all__ = ["Message", "MessageList", "normalize_messages", "Role", "ResponseFormat"]
