# Backward-compatibility shim — implementation moved to bizniz.core.client
from bizniz.core.client import BaseAIClient

__all__ = ["BaseAIClient"]

# Re-export types that were previously imported through this module
from bizniz.core.types import ResponseFormat, Message, MessageList
