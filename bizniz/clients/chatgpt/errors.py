class OpenAIClientError(Exception):
    """Base client error."""


class OpenAIRateLimit(OpenAIClientError):
    pass


class OpenAIInsufficientFunds(OpenAIClientError):
    """Raised when the API account has no funds/quota remaining.

    This is a terminal error — retrying will not help.
    """
    pass


class OpenAIAuthError(OpenAIClientError):
    pass


class OpenAIInvalidRequest(OpenAIClientError):
    pass
