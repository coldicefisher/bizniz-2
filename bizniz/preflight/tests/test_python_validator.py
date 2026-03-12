"""Tests for Python pre-flight validator."""

import pytest
from unittest.mock import MagicMock, patch

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


@pytest.fixture(autouse=True)
def mock_pypi():
    """Disable PyPI network calls in all tests by default."""
    with patch(
        "bizniz.preflight.python_validator._pypi_package_exists", return_value=False
    ) as m:
        yield m


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

    def test_missing_relative_import_normalized_and_resolved(self, validator):
        """Relative import is normalized to absolute; parent __init__.py satisfies it."""
        files = {
            "pkg/__init__.py": "",
            "pkg/app.py": "from .errors import NotFoundError\n",
        }
        result = validator.validate(files, [])
        # Relative import gets rewritten to absolute
        assert len(result.import_rewrites) == 1
        assert result.import_rewrites[0].new_import == "pkg.errors"
        # pkg/__init__.py exists so pkg.errors is considered valid (attr of __init__)
        assert result.passed

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

    def test_relative_imports_normalized_to_absolute(self, validator):
        """Relative imports should be rewritten to absolute during preflight."""
        files = {
            "myapp/__init__.py": "",
            "myapp/models/__init__.py": "",
            "myapp/models/service.py": "class Service: pass\n",
            "myapp/api/__init__.py": "",
            "myapp/api/routers/__init__.py": "",
            "myapp/api/routers/services.py": (
                "from ...models.service import Service\n"
                "from ..deps import get_db\n"
            ),
        }
        result = validator.validate(files, [])
        # Check the file was rewritten
        rewritten = files["myapp/api/routers/services.py"]
        assert "from myapp.models.service import Service" in rewritten
        assert "from myapp.api.deps import get_db" in rewritten
        # Check rewrites are tracked
        assert len(result.import_rewrites) == 2

    def test_relative_import_wrong_level_normalized(self, validator):
        """Wrong-level relative import gets resolved to correct absolute path."""
        files = {
            "myapp/__init__.py": "",
            "myapp/models/__init__.py": "",
            "myapp/models/service.py": "class Service: pass\n",
            "myapp/api/__init__.py": "",
            "myapp/api/routers/__init__.py": "",
            # Uses .. (level 2) when ... (level 3) was needed
            "myapp/api/routers/services.py": (
                "from ..models.service import Service\n"
            ),
        }
        result = validator.validate(files, [])
        rewritten = files["myapp/api/routers/services.py"]
        # .. from myapp/api/routers/ resolves to myapp.api.models.service
        assert "from myapp.api.models.service import Service" in rewritten

    def test_no_stub_when_leaf_module_exists_elsewhere(self, validator):
        """Skip stubbing when the leaf module exists at a different path."""
        files = {
            "myapp/__init__.py": "",
            "myapp/models/__init__.py": "",
            "myapp/models/appointment.py": "class Appointment: pass\n",
            "myapp/api/__init__.py": "",
            "myapp/api/routers/__init__.py": "",
            # Router wrongly imports from myapp.api.models.appointment
            "myapp/api/routers/appointments.py": (
                "from myapp.api.models.appointment import Appointment\n"
            ),
        }
        result = validator.validate(files, [])
        stub_paths = [s.filepath for s in result.stubs_created]
        # Should NOT create a stub at myapp/api/models/appointment.py
        # because the real module exists at myapp/models/appointment.py
        assert "myapp/api/models/appointment.py" not in stub_paths

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
            "app.py": "from pkg.errors import ValidationError, NotFoundError\n",
        }
        result = validator.validate(files, [])
        stub = next(s for s in result.stubs_created if s.filepath == "pkg/errors.py")
        assert "class ValidationError(Exception):" in stub.content
        assert "class NotFoundError(Exception):" in stub.content

    def test_regular_class_stub(self, validator):
        files = {
            "app.py": "from pkg.models import User\n",
        }
        result = validator.validate(files, [])
        stub = next(s for s in result.stubs_created if s.filepath == "pkg/models.py")
        assert "class User:" in stub.content
        assert "Exception" not in stub.content

    def test_function_stub(self, validator):
        files = {
            "app.py": "from pkg.utils import calculate_total\n",
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
            "app.py": "from pkg.missing import Foo\n",
        }
        result = validator.validate(files, [])
        summary = result.summary()
        assert "auto-stub" in summary.lower() or "Auto-fixed" in summary

    def test_no_stub_shadowing_declared_dep(self, validator):
        """Never create a stub file that shadows a declared dependency."""
        files = {
            "myapp/__init__.py": "",
            "myapp/api.py": "from pydantic import BaseModel\n",
        }
        result = validator.validate(files, ["pydantic"])
        stub_paths = [s.filepath for s in result.stubs_created]
        assert "pydantic.py" not in stub_paths

    def test_no_stub_shadowing_stdlib(self, validator):
        """Never create a stub file that shadows a stdlib module."""
        files = {
            "myapp/__init__.py": "",
            "myapp/api.py": "from json import dumps\n",
        }
        result = validator.validate(files, [])
        stub_paths = [s.filepath for s in result.stubs_created]
        assert "json.py" not in stub_paths

    def test_no_stub_shadowing_common_alias(self, validator):
        """Never create a stub file that shadows a common alias package."""
        files = {
            "app.py": "import yaml\n",
        }
        result = validator.validate(files, [])
        stub_paths = [s.filepath for s in result.stubs_created]
        assert "yaml.py" not in stub_paths


class TestPyPILookup:

    def test_pypi_package_added_to_install(self, validator, mock_pypi):
        """Package found on PyPI gets added to packages_to_install, not stubbed."""
        mock_pypi.return_value = True
        files = {
            "app.py": "from stripe import Charge\n",
        }
        result = validator.validate(files, [])
        assert "stripe" in result.packages_to_install
        stub_paths = [s.filepath for s in result.stubs_created]
        assert "stripe.py" not in stub_paths

    def test_pypi_lookup_failure_falls_back_to_stub(self, validator, mock_pypi):
        """When PyPI lookup fails, fall back to stubbing."""
        mock_pypi.return_value = None  # network error
        files = {
            "app.py": "from somepkg import Foo\n",
        }
        result = validator.validate(files, [])
        assert len(result.packages_to_install) == 0
        assert len(result.stubs_created) >= 1

    def test_ambiguous_name_flagged_not_installed(self, validator, mock_pypi):
        """Ambiguous name (common local module name) on PyPI gets flagged, not installed."""
        mock_pypi.return_value = True
        files = {
            "app.py": "from models import User\n",
        }
        result = validator.validate(files, [])
        assert "models" not in result.packages_to_install
        assert any(i.issue == "ambiguous_import" for i in result.issues)

    def test_ambiguous_name_not_on_pypi_gets_stubbed(self, validator, mock_pypi):
        """Ambiguous name not on PyPI gets stubbed normally."""
        mock_pypi.return_value = False
        files = {
            "app.py": "from models import User\n",
        }
        result = validator.validate(files, [])
        assert len(result.stubs_created) >= 1
        stub_paths = [s.filepath for s in result.stubs_created]
        assert "models.py" in stub_paths

    def test_packages_to_install_deduped(self, validator, mock_pypi):
        """Multiple files importing same PyPI package only lists it once."""
        mock_pypi.return_value = True
        files = {
            "app.py": "from stripe import Charge\n",
            "billing.py": "from stripe import Customer\n",
        }
        result = validator.validate(files, [])
        assert result.packages_to_install.count("stripe") == 1

    def test_pydantic_not_stubbed_when_detected_via_pypi(self, validator, mock_pypi):
        """pydantic import gets added to packages_to_install, never stubbed."""
        mock_pypi.return_value = True
        files = {
            "myapp/__init__.py": "",
            "myapp/models.py": "from pydantic import BaseModel\n",
        }
        result = validator.validate(files, [])
        assert "pydantic" in result.packages_to_install
        stub_paths = [s.filepath for s in result.stubs_created]
        assert "pydantic.py" not in stub_paths
