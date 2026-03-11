"""Tests for C# pre-flight validator."""

import pytest
from unittest.mock import MagicMock

from bizniz.preflight.csharp_validator import CSharpPreflightValidator
from bizniz.workspace.base_workspace import BaseWorkspace


@pytest.fixture
def workspace():
    ws = MagicMock(spec=BaseWorkspace)
    ws.list_relative_files.return_value = []
    ws.read_file.return_value = ""
    return ws


@pytest.fixture
def validator(workspace):
    return CSharpPreflightValidator(workspace)


class TestUsingResolution:

    def test_system_namespace_passes(self, validator):
        files = {
            "Program.cs": "using System;\nusing System.Collections.Generic;\n\nnamespace MyApp\n{\n}\n",
        }
        result = validator.validate(files, [])
        assert result.passed

    def test_microsoft_namespace_passes(self, validator):
        files = {
            "Startup.cs": "using Microsoft.AspNetCore.Builder;\n\nnamespace MyApp\n{\n}\n",
        }
        result = validator.validate(files, [])
        assert result.passed

    def test_same_project_namespace_resolves(self, validator):
        files = {
            "Models/User.cs": "namespace MyApp.Models\n{\n    public class User { }\n}\n",
            "Services/UserService.cs": "using MyApp.Models;\n\nnamespace MyApp.Services\n{\n    public class UserService { }\n}\n",
        }
        result = validator.validate(files, [])
        assert result.passed

    def test_missing_namespace_creates_stub(self, validator):
        files = {
            "Services/BookingService.cs": "using MyApp.Domain.Errors;\n\nnamespace MyApp.Services\n{\n}\n",
        }
        result = validator.validate(files, [])
        assert len(result.stubs_created) == 1
        assert result.stubs_created[0].filepath.endswith(".cs")
        assert "namespace MyApp.Domain.Errors" in result.stubs_created[0].content

    def test_nuget_dependency_passes(self, validator):
        files = {
            "App.cs": "using Newtonsoft.Json;\n\nnamespace MyApp\n{\n}\n",
        }
        result = validator.validate(files, ["Newtonsoft.Json"])
        assert result.passed

    def test_xunit_passes(self, validator):
        files = {
            "Tests.cs": "using Xunit;\n\nnamespace MyApp.Tests\n{\n}\n",
        }
        result = validator.validate(files, [])
        assert result.passed


class TestStubGeneration:

    def test_exception_class_stub(self, validator):
        files = {
            "Services/BookingService.cs": (
                "using Acme.Domain.Errors;\n\n"
                "namespace Acme.Services\n{\n"
                "    public class BookingService\n    {\n"
                "    }\n}\n"
            ),
        }
        result = validator.validate(files, [])
        # Should create a stub for Acme.Domain.Errors namespace
        stubs = [s for s in result.stubs_created if "Errors" in s.filepath]
        assert len(stubs) >= 1
        assert "namespace Acme.Domain.Errors" in stubs[0].content


class TestPreflightResult:

    def test_summary_shows_language(self, validator):
        files = {
            "App.cs": "using System;\n\nnamespace MyApp\n{\n}\n",
        }
        result = validator.validate(files, [])
        assert "csharp" in result.summary()
