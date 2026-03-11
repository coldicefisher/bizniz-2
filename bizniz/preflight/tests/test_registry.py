"""Tests for the validated language registry."""

import pytest
from unittest.mock import MagicMock

from bizniz.preflight.registry import (
    get_validator,
    is_validated_language,
    VALIDATED_LANGUAGES,
)
from bizniz.preflight.python_validator import PythonPreflightValidator
from bizniz.preflight.typescript_validator import TypeScriptPreflightValidator
from bizniz.preflight.javascript_validator import JavaScriptPreflightValidator
from bizniz.preflight.csharp_validator import CSharpPreflightValidator
from bizniz.workspace.base_workspace import BaseWorkspace


@pytest.fixture
def workspace():
    return MagicMock(spec=BaseWorkspace)


class TestGetValidator:

    def test_python(self, workspace):
        v = get_validator("python", workspace)
        assert isinstance(v, PythonPreflightValidator)

    def test_typescript(self, workspace):
        v = get_validator("typescript", workspace)
        assert isinstance(v, TypeScriptPreflightValidator)

    def test_javascript(self, workspace):
        v = get_validator("javascript", workspace)
        assert isinstance(v, JavaScriptPreflightValidator)

    def test_csharp(self, workspace):
        v = get_validator("csharp", workspace)
        assert isinstance(v, CSharpPreflightValidator)

    def test_aliases(self, workspace):
        assert isinstance(get_validator("py", workspace), PythonPreflightValidator)
        assert isinstance(get_validator("ts", workspace), TypeScriptPreflightValidator)
        assert isinstance(get_validator("tsx", workspace), TypeScriptPreflightValidator)
        assert isinstance(get_validator("js", workspace), JavaScriptPreflightValidator)
        assert isinstance(get_validator("c#", workspace), CSharpPreflightValidator)
        assert isinstance(get_validator("cs", workspace), CSharpPreflightValidator)
        assert isinstance(get_validator(".net", workspace), CSharpPreflightValidator)

    def test_case_insensitive(self, workspace):
        assert isinstance(get_validator("Python", workspace), PythonPreflightValidator)
        assert isinstance(get_validator("TYPESCRIPT", workspace), TypeScriptPreflightValidator)

    def test_unknown_returns_none(self, workspace):
        assert get_validator("rust", workspace) is None
        assert get_validator("go", workspace) is None

    def test_unvalidated_still_works(self, workspace):
        # Unvalidated languages return None — they work, just no guardrails
        assert get_validator("ruby", workspace) is None


class TestIsValidatedLanguage:

    def test_validated(self):
        assert is_validated_language("python")
        assert is_validated_language("typescript")
        assert is_validated_language("javascript")
        assert is_validated_language("csharp")

    def test_aliases(self):
        assert is_validated_language("py")
        assert is_validated_language("ts")
        assert is_validated_language("c#")

    def test_unvalidated(self):
        assert not is_validated_language("rust")
        assert not is_validated_language("go")
        assert not is_validated_language("ruby")


class TestValidatedLanguagesList:

    def test_contains_all_four(self):
        assert "python" in VALIDATED_LANGUAGES
        assert "typescript" in VALIDATED_LANGUAGES
        assert "javascript" in VALIDATED_LANGUAGES
        assert "csharp" in VALIDATED_LANGUAGES
        assert len(VALIDATED_LANGUAGES) == 4
