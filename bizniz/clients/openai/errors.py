from bizniz.clients.errors import AIInsufficientFunds, AIContextLengthExceeded


class OpenAIClientError(Exception):
    """Base client error."""


class OpenAIRateLimit(OpenAIClientError):
    pass


class OpenAIInsufficientFunds(OpenAIClientError, AIInsufficientFunds):
    """Raised when the API account has no funds/quota remaining.

    This is a terminal error — retrying will not help.
    """
    pass


class OpenAIAuthError(OpenAIClientError):
    pass


class OpenAIInvalidRequest(OpenAIClientError):
    pass


class OpenAIContextLengthExceeded(OpenAIInvalidRequest, AIContextLengthExceeded):
    """Input exceeds the model's context window."""
    pass
