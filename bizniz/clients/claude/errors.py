"""
Claude client error types.

Mirrors the OpenAI error hierarchy for consistent handling
across the pipeline.
"""


from bizniz.clients.errors import AIInsufficientFunds, AIContextLengthExceeded


class ClaudeClientError(Exception):
    """Base error for Claude client operations."""
    pass


class ClaudeRateLimit(ClaudeClientError):
    """Rate limit hit — retriable after backoff."""
    pass


class ClaudeInsufficientFunds(ClaudeClientError, AIInsufficientFunds):
    """Account has insufficient funds — terminal, stop immediately."""
    pass


class ClaudeAuthError(ClaudeClientError):
    """Authentication failure — bad or missing API key."""
    pass


class ClaudeInvalidRequest(ClaudeClientError):
    """Bad request — malformed input, too many tokens, etc."""
    pass


class ClaudeContextLengthExceeded(ClaudeInvalidRequest, AIContextLengthExceeded):
    """Input exceeds the model's context window."""
    pass
