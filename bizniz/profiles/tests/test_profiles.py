"""Tests for the service-type profile registry."""
from types import SimpleNamespace

import pytest

from bizniz.profiles import (
    SERVICE_PROFILES,
    UnknownServiceTypeError,
    documenter_for,
    has_profile,
    profile_for,
)
from bizniz.documenters.python_ast import PythonAstDocumenter
from bizniz.documenters.typescript_ast import TypeScriptAstDocumenter


def _service(service_type, framework, language="python"):
    return SimpleNamespace(
        service_type=service_type, framework=framework, language=language,
    )


def test_known_combinations_resolve():
    p = profile_for(_service("backend", "fastapi"))
    assert p.language == "python"
    assert p.skeleton == "fastapi"
    assert p.contract_format == "openapi"
    assert p.test_runner == "pytest"
    assert p.validator == ["python", "-m", "pyright", "app/"]


def test_react_resolves():
    p = profile_for(_service("frontend", "react"))
    assert p.language == "typescript"
    assert p.skeleton == "react"
    assert p.validator == ["npx", "tsc", "--noEmit"]


def test_angular_resolves():
    p = profile_for(_service("frontend", "angular"))
    assert p.language == "typescript"
    assert p.skeleton == "angular"


def test_case_insensitive_lookup():
    # Architects emit titlecase sometimes; lookup should be tolerant.
    p = profile_for(_service("Backend", "FastAPI"))
    assert p.framework == "fastapi"


def test_unknown_combination_raises():
    with pytest.raises(UnknownServiceTypeError) as exc:
        profile_for(_service("backend", "rails"))
    # Message should be actionable.
    assert "rails" in str(exc.value).lower()
    assert "profile" in str(exc.value).lower()


def test_has_profile_helper():
    assert has_profile("backend", "fastapi")
    assert not has_profile("backend", "rails")


def test_documenter_for_python_returns_class():
    doc = documenter_for(_service("backend", "fastapi"))
    assert doc is PythonAstDocumenter


def test_documenter_for_typescript_returns_class():
    doc = documenter_for(_service("frontend", "react"))
    assert doc is TypeScriptAstDocumenter


def test_documenter_for_unknown_returns_none():
    """Unknown combinations soft-fail in documenter_for so callers
    can degrade gracefully (no doc persistence) rather than crash."""
    doc = documenter_for(_service("backend", "rails"))
    assert doc is None


def test_profiles_are_immutable():
    """Profiles are frozen dataclasses so they can't be mutated at
    runtime by accident."""
    p = SERVICE_PROFILES[("backend", "fastapi")]
    with pytest.raises(Exception):
        p.skeleton = "different"
