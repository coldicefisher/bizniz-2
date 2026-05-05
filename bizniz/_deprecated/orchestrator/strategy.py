"""
Coding strategies for the orchestrator.
"""

from enum import Enum


class CodingStrategy(str, Enum):
    """Strategy for code generation order in the orchestrator."""

    TDD = "tdd"
    """Test-Driven Development: generate tests first from the spec,
    then generate code to pass them. Debug loop fixes code only."""

    CODE_FIRST = "code_first"
    """Code-first: generate code, then tests, debug loop can fix either.
    Used as a fallback when TDD fails."""
