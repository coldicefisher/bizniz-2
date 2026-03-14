# Backward-compatibility shim — implementation moved to bizniz.clients.openai
from bizniz.clients.openai.errors import (
    OpenAIClientError,
    OpenAIRateLimit,
    OpenAIInsufficientFunds,
    OpenAIAuthError,
    OpenAIInvalidRequest,
    OpenAIContextLengthExceeded,
)
