"""Tests for the collection-error classifier in CodingOrchestrator.

The classifier decides whether a pytest collection failure (exit code 2/4)
is caused by broken SOURCE code (repair source) or a bad test file
(regenerate test). Misclassification was the bug that wasted iterations on
the 2026-04-29 backend run before this hardening.
"""
import pytest

from bizniz.orchestrator.coding_orchestrator import (
    CodingOrchestrator,
    _is_config_file,
)


@pytest.fixture
def classify():
    """Return a bound _is_source_import_error for a stub orchestrator instance.

    We don't need a fully wired CodingOrchestrator — the classifier doesn't
    touch any instance state beyond ``self``. Construct an empty shim.
    """
    inst = CodingOrchestrator.__new__(CodingOrchestrator)
    return inst._is_source_import_error


# ── Source-side signals (should return True) ─────────────────────────────────

def test_traceback_frame_in_source_file(classify):
    err = (
        '____________ ERROR collecting tests/test_app.py ____________\n'
        'tests/test_app.py:1: in <module>\n'
        '    from pet_groomer.app import app\n'
        'pet_groomer/app.py:7: NameError: name \'FastAPI\' is not defined\n'
    )
    current_files = {"pet_groomer/app.py": "..."}
    assert classify(err, current_files) is True


def test_traceback_with_File_quoted_path(classify):
    err = (
        'Traceback (most recent call last):\n'
        '  File "/workspace/pet_groomer/app.py", line 7, in <module>\n'
        '    app = FastAPI()\n'
        'NameError: name \'FastAPI\' is not defined\n'
    )
    current_files = {"pet_groomer/app.py": "..."}
    assert classify(err, current_files) is True


def test_first_failing_import_matches_our_module(classify):
    err = (
        '____________ ERROR collecting tests/test_routes.py ____________\n'
        'tests/test_routes.py:2: in <module>\n'
        '> from pet_groomer.routers.services_router import get_services\n'
        'E   ImportError: cannot import name \'get_services\'\n'
    )
    current_files = {"pet_groomer/routers/services_router.py": "..."}
    assert classify(err, current_files) is True


def test_module_not_found_referencing_our_path(classify):
    err = (
        'ModuleNotFoundError: No module named "pet_groomer.repositories.datastore"\n'
        'pet_groomer/repositories/datastore.py: not on sys.path\n'
    )
    # The path mention + ImportError-class signal both fire.
    current_files = {"pet_groomer/repositories/datastore.py": "..."}
    assert classify(err, current_files) is True


# ── Test-side signals (should return False) ──────────────────────────────────

def test_undefined_fixture_in_test(classify):
    """A fixture name typo in the test file is a test problem, not source."""
    err = (
        '____________ ERROR collecting tests/test_models.py ____________\n'
        'file /workspace/tests/test_models.py, line 12\n'
        '  def test_something(undefined_fixture):\n'
        'E       fixture \'undefined_fixture\' not found\n'
    )
    current_files = {"pet_groomer/models/service.py": "..."}
    assert classify(err, current_files) is False


def test_test_imports_unrelated_module(classify):
    """Test imports a third-party lib that isn't installed — not our source."""
    err = (
        '____________ ERROR collecting tests/test_x.py ____________\n'
        'tests/test_x.py:1: in <module>\n'
        '> import some_third_party_lib\n'
        'E   ModuleNotFoundError: No module named \'some_third_party_lib\'\n'
    )
    current_files = {"pet_groomer/models/service.py": "..."}
    assert classify(err, current_files) is False


def test_traceback_only_in_test_file(classify):
    """Frame is in tests/ — that's a test problem."""
    err = (
        'Traceback (most recent call last):\n'
        '  File "/workspace/tests/test_routes.py", line 5, in <module>\n'
        '    setup_invalid_thing()\n'
        'NameError: setup_invalid_thing\n'
    )
    current_files = {
        "pet_groomer/routers/services_router.py": "...",
        "tests/test_routes.py": "...",
    }
    assert classify(err, current_files) is False


def test_empty_error_returns_false(classify):
    assert classify("", {"pet_groomer/app.py": "..."}) is False
    assert classify(None, {}) is False


def test_no_current_files_returns_false(classify):
    err = "tests/test_x.py:5: in <module>\n    from foo import bar\nE   ImportError"
    assert classify(err, {}) is False


# ── Config-file allowlist (Bug 3c) ────────────────────────────────────────────

def test_config_allowlist_python_files():
    assert _is_config_file("pyproject.toml")
    assert _is_config_file("backend/pyproject.toml")
    assert _is_config_file("setup.cfg")
    assert _is_config_file("setup.py")
    assert _is_config_file("pytest.ini")
    assert _is_config_file("requirements.txt")
    assert _is_config_file("backend/requirements.txt")


def test_config_allowlist_js_ts_files():
    assert _is_config_file("package.json")
    assert _is_config_file("frontend/package.json")
    assert _is_config_file("tsconfig.json")
    assert _is_config_file("tsconfig.app.json")
    assert _is_config_file("jest.config.js")
    assert _is_config_file("jest.config.ts")
    assert _is_config_file("frontend/jest.config.cjs")
    assert _is_config_file("vite.config.ts")
    assert _is_config_file("angular.json")


def test_config_allowlist_dockerfiles():
    assert _is_config_file("Dockerfile")
    assert _is_config_file("infra/development/backend/Dockerfile")
    assert _is_config_file("docker-compose.yml")
    assert _is_config_file("docker-compose.yaml")


def test_config_allowlist_excludes_app_code():
    assert not _is_config_file("app/main.py")
    assert not _is_config_file("src/App.tsx")
    assert not _is_config_file("tests/test_models.py")
    assert not _is_config_file("pet_groomer/models/service.py")


def test_config_allowlist_excludes_lockfiles():
    """Lockfiles are not in the allowlist; AI shouldn't rewrite them."""
    assert not _is_config_file("package-lock.json")
    assert not _is_config_file("yarn.lock")
    assert not _is_config_file("Cargo.lock")


def test_config_allowlist_handles_empty_string():
    assert not _is_config_file("")
