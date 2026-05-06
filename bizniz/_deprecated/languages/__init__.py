"""
bizniz.languages — language strategy pattern for the pipeline.

Each language provides its own strategy that encapsulates prompts,
file conventions, import scanning, and package management.
"""

from bizniz._deprecated.languages.base import LanguageStrategy
from bizniz._deprecated.languages.python import PythonStrategy
from bizniz._deprecated.languages.typescript import TypeScriptStrategy


_STRATEGIES = {
    "python": PythonStrategy,
    "typescript": TypeScriptStrategy,
}


def get_language_strategy(language: str) -> LanguageStrategy:
    """Return the strategy for the given language name.

    Falls back to PythonStrategy for unknown languages.
    """
    cls = _STRATEGIES.get(language, PythonStrategy)
    return cls()


__all__ = [
    "LanguageStrategy",
    "PythonStrategy",
    "TypeScriptStrategy",
    "get_language_strategy",
]
