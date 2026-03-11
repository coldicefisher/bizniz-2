"""Tests for Python pre-flight validator."""

import pytest
from unittest.mock import MagicMock

from bizniz.preflight.python_validator import PythonPreflightValidator
from bizniz.workspace.base_workspace import BaseWorkspace


@pytest.fixture
def workspace():
    ws = MagicMock(spec=BaseWorkspace)
    ws.list_relative_files.return_value = []
    ws.read_file.return_value = ""
    return ws


@pytest.fixture
def validator(workspace):
    return PythonPreflightValidator(workspace)


class TestImportResolution:

    def test_stdlib_imports_pass(self, validator):
        files = {
            "app.py": "import os\nimport json\nfrom pathlib import Path\n",
        }
        result = validator.validate(files, [])
        assert result.passed
        assert result.files_checked == 1

    def test_declared_dependency_passes(self, validator):
        files = {
            "app.py": "from fastapi import FastAPI\nimport pydantic\n",
        }
        result = validator.validate(files, ["fastapi", "pydantic"])
        assert result.passed

    def test_missing_absolute_import_auto_stubbed(self, validator):
        files = {
            "app.py": "from nonexistent_package import Foo\n",
        }
        result = validator.validate(files, [])
        # Auto-stub kicks in and creates the missing module
        assert result.passed  # No unresolved issues
        assert len(result.stubs_created) >= 1
        stub_paths = [s.filepath for s in result.stubs_created]
        assert "nonexistent_package.py" in stub_paths

    def test_relative_import_resolves(self, validator):
        files = {
            "pkg/__init__.py": "",
            "pkg/models.py": "class Foo: pass\n",
            "pkg/app.py": "from .models import Foo\n",
        }
        result = validator.validate(files, [])
        assert result.passed

    def test_missing_relative_import_creates_stub(self, validator):
        files = {
            "pkg/__init__.py": "",
            "pkg/app.py": "from .errors import NotFoundError\n",
        }
        result = validator.validate(files, [])
        assert len(result.stubs_created) == 1
        assert result.stubs_created[0].filepath == "pkg/errors.py"
        assert "NotFoundError" in result.stubs_created[0].content
        assert "Exception" in result.stubs_created[0].content

    def test_absolute_import_to_workspace_module(self, validator):
        files = {
            "myapp/__init__.py": "",
            "myapp/models.py": "class User: pass\n",
            "myapp/api.py": "from myapp.models import User\n",
        }
        result = validator.validate(files, [])
        assert result.passed

    def test_missing_absolute_import_creates_stub(self, validator):
        files = {
            "myapp/__init__.py": "",
            "myapp/api.py": "from myapp.domain.errors import NotFoundError\n",
        }
        result = validator.validate(files, [])
        assert len(result.stubs_created) >= 1
        stub_paths = [s.filepath for s in result.stubs_created]
        assert any("errors.py" in p for p in stub_paths)

    def test_dep_with_version_specifier(self, validator):
        files = {
            "app.py": "import fastapi\n",
        }
        result = validator.validate(files, ["fastapi>=0.100.0"])
        assert result.passed

    def test_dep_with_extras(self, validator):
        files = {
            "app.py": "import uvicorn\n",
        }
        result = validator.validate(files, ["uvicorn[standard]"])
        assert result.passed

    def test_common_alias_recognized(self, validator):
        files = {
            "app.py": "import yaml\n",
        }
        result = validator.validate(files, [])
        assert result.passed  # yaml -> PyYAML alias


class TestInitFileGeneration:

    def test_creates_missing_init(self, validator):
        files = {
            "pkg/models.py": "class Foo: pass\n",
        }
        result = validator.validate(files, [])
        init_stubs = [s for s in result.stubs_created if "__init__" in s.filepath]
        assert len(init_stubs) == 1
        assert init_stubs[0].filepath == "pkg/__init__.py"

    def test_nested_packages_get_inits(self, validator):
        files = {
            "pkg/sub/deep/models.py": "class Foo: pass\n",
        }
        result = validator.validate(files, [])
        init_stubs = [s for s in result.stubs_created if "__init__" in s.filepath]
        init_paths = {s.filepath for s in init_stubs}
        assert "pkg/__init__.py" in init_paths
        assert "pkg/sub/__init__.py" in init_paths
        assert "pkg/sub/deep/__init__.py" in init_paths

    def test_existing_init_not_duplicated(self, validator):
        files = {
            "pkg/__init__.py": "",
            "pkg/models.py": "class Foo: pass\n",
        }
        result = validator.validate(files, [])
        init_stubs = [s for s in result.stubs_created if "__init__" in s.filepath]
        assert len(init_stubs) == 0


class TestStubGeneration:

    def test_error_class_stub(self, validator):
        files = {
            "pkg/__init__.py": "",
            "pkg/app.py": "from .errors import ValidationError, NotFoundError\n",
        }
        result = validator.validate(files, [])
        stub = next(s for s in result.stubs_created if s.filepath == "pkg/errors.py")
        assert "class ValidationError(Exception):" in stub.content
        assert "class NotFoundError(Exception):" in stub.content

    def test_regular_class_stub(self, validator):
        files = {
            "pkg/__init__.py": "",
            "pkg/app.py": "from .models import User\n",
        }
        result = validator.validate(files, [])
        stub = next(s for s in result.stubs_created if s.filepath == "pkg/models.py")
        assert "class User:" in stub.content
        assert "Exception" not in stub.content

    def test_function_stub(self, validator):
        files = {
            "pkg/__init__.py": "",
            "pkg/app.py": "from .utils import calculate_total\n",
        }
        result = validator.validate(files, [])
        stub = next(s for s in result.stubs_created if s.filepath == "pkg/utils.py")
        assert "def calculate_total" in stub.content


class TestPreflightResult:

    def test_passed_when_no_issues(self, validator):
        files = {
            "app.py": "import os\n",
        }
        result = validator.validate(files, [])
        assert result.passed
        assert result.issues_fixed == 0

    def test_summary_output(self, validator):
        files = {
            "pkg/__init__.py": "",
            "pkg/app.py": "from .missing import Foo\n",
        }
        result = validator.validate(files, [])
        summary = result.summary()
        assert "auto-stub" in summary.lower() or "Auto-fixed" in summary
