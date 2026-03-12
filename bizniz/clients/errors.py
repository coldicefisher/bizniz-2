"""
Base AI client errors.

Provider-specific errors (OpenAI, Claude) should inherit from these
so the pipeline can catch them generically.
"""


class AIClientError(Exception):
    """Base error for all AI client operations."""
    pass


class AIInsufficientFunds(AIClientError):
    """Account has insufficient funds — terminal, stop immediately.

    Both OpenAIInsufficientFunds and ClaudeInsufficientFunds inherit
    from this so the orchestrator can catch either with one except clause.
    """
    pass


class AIContextLengthExceeded(AIClientError):
    """Input exceeds the model's context window.

    Caught by the tool loop to trim messages and retry, rather than
    burning all retry attempts on the same oversized payload.
    """
    pass
