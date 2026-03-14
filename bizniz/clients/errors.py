# Backward-compatibility shim — implementation moved to bizniz.core.errors
from bizniz.core.errors import AIClientError, AIInsufficientFunds, AIContextLengthExceeded

__all__ = ["AIClientError", "AIInsufficientFunds", "AIContextLengthExceeded"]
