"""
Gemini client error types.

Mirrors the Claude/OpenAI error hierarchy for consistent handling
across the pipeline.
"""

from bizniz.clients.errors import AIInsufficientFunds, AIContextLengthExceeded


class GeminiClientError(Exception):
    """Base error for Gemini client operations."""
    pass


class GeminiRateLimit(GeminiClientError):
    """Rate limit hit — retriable after backoff."""
    pass


class GeminiInsufficientFunds(GeminiClientError, AIInsufficientFunds):
    """Account has insufficient funds — terminal, stop immediately."""
    pass


class GeminiAuthError(GeminiClientError):
    """Authentication failure — bad or missing API key."""
    pass


class GeminiInvalidRequest(GeminiClientError):
    """Bad request — malformed input, too many tokens, etc."""
    pass


class GeminiContextLengthExceeded(GeminiInvalidRequest, AIContextLengthExceeded):
    """Input exceeds the model's context window."""
    pass
