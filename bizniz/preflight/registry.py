"""
Validated language registry.

Maps language identifiers to their pre-flight validators.
Only languages with validators get structural pre-flight checks.
Unvalidated languages still work — they just don't get the guardrails.
"""

from typing import Dict, Optional, Type

from bizniz.preflight.base_validator import BasePreflightValidator
from bizniz.preflight.python_validator import PythonPreflightValidator
from bizniz.preflight.typescript_validator import TypeScriptPreflightValidator
from bizniz.preflight.javascript_validator import JavaScriptPreflightValidator
from bizniz.preflight.csharp_validator import CSharpPreflightValidator
from bizniz.workspace.base_workspace import BaseWorkspace


# Validated languages — these get pre-flight structural checks
_VALIDATORS: Dict[str, Type[BasePreflightValidator]] = {
    "python": PythonPreflightValidator,
    "typescript": TypeScriptPreflightValidator,
    "javascript": JavaScriptPreflightValidator,
    "csharp": CSharpPreflightValidator,
}

# Language aliases
_ALIASES: Dict[str, str] = {
    "py": "python",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "c#": "csharp",
    "cs": "csharp",
    ".net": "csharp",
    "dotnet": "csharp",
}

VALIDATED_LANGUAGES = list(_VALIDATORS.keys())


def get_validator(
    language: str, workspace: BaseWorkspace
) -> Optional[BasePreflightValidator]:
    """
    Get a pre-flight validator for the given language.

    Returns None for unvalidated languages — the system still works,
    it just doesn't get structural pre-flight checks.
    """
    lang = _ALIASES.get(language.lower(), language.lower())
    validator_cls = _VALIDATORS.get(lang)
    if validator_cls is None:
        return None
    return validator_cls(workspace)


def is_validated_language(language: str) -> bool:
    """Check if a language has pre-flight validation support."""
    lang = _ALIASES.get(language.lower(), language.lower())
    return lang in _VALIDATORS
