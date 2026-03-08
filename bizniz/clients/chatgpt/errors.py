class OpenAIClientError(Exception):
    """Base client error."""


class OpenAIRateLimit(OpenAIClientError):
    pass


class OpenAIAuthError(OpenAIClientError):
    pass


class OpenAIInvalidRequest(OpenAIClientError):
    pass
