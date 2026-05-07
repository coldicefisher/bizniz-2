"""Tests for the deterministic symbol validator."""
from pathlib import Path

import pytest

from bizniz.coder.symbol_validator import (
    SymbolValidationReport,
    validate_files,
    validate_python_file,
)


def _ws(tmp_path: Path) -> Path:
    """Create a workspace root with a requirements.txt so third-party
    detection has a known set."""
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.110.0\n"
        "pydantic>=2.0\n"
        "httpx\n"
        "python-jose[cryptography]\n"
    )
    return tmp_path


# ── Resolve helpers ────────────────────────────────────────────────────


class TestResolves:
    def test_stdlib_resolves(self, tmp_path):
        ws = _ws(tmp_path)
        f = ws / "x.py"
        f.write_text("import os\nimport sys\nfrom pathlib import Path\n")
        report = validate_python_file(f, ws)
        assert report.passed
        assert report.resolved_count == 3

    def test_third_party_in_requirements_resolves(self, tmp_path):
        ws = _ws(tmp_path)
        f = ws / "x.py"
        f.write_text(
            "from fastapi import APIRouter\n"
            "from pydantic import BaseModel\n"
        )
        report = validate_python_file(f, ws)
        assert report.passed

    def test_dash_in_package_name_normalized(self, tmp_path):
        # python-jose appears as "from jose import jwt" in code; the
        # alias map should resolve.
        ws = _ws(tmp_path)
        f = ws / "x.py"
        f.write_text("from jose import jwt\n")
        report = validate_python_file(f, ws)
        assert report.passed

    def test_local_module_resolves(self, tmp_path):
        ws = _ws(tmp_path)
        # Create a local package
        (ws / "app").mkdir()
        (ws / "app" / "__init__.py").write_text("")
        (ws / "app" / "models.py").write_text("class User: pass\n")
        f = ws / "x.py"
        f.write_text("from app.models import User\n")
        report = validate_python_file(f, ws)
        assert report.passed

    def test_relative_import_skipped(self, tmp_path):
        ws = _ws(tmp_path)
        (ws / "pkg").mkdir()
        (ws / "pkg" / "__init__.py").write_text("")
        f = ws / "pkg" / "y.py"
        f.write_text("from . import sibling\n")
        report = validate_python_file(f, ws)
        # Relative imports are skipped (resolved at runtime by package context)
        assert report.passed


# ── Hallucination detection ────────────────────────────────────────────


class TestHallucinations:
    def test_unknown_top_level_import_flagged(self, tmp_path):
        ws = _ws(tmp_path)
        f = ws / "x.py"
        f.write_text("import nonexistent_lib\n")
        report = validate_python_file(f, ws)
        assert not report.passed
        assert len(report.unresolved) == 1
        assert report.unresolved[0].symbol == "nonexistent_lib"
        assert report.unresolved[0].kind == "import"

    def test_unknown_from_import_flagged(self, tmp_path):
        ws = _ws(tmp_path)
        f = ws / "x.py"
        f.write_text("from fake_pkg import something\n")
        report = validate_python_file(f, ws)
        assert not report.passed
        u = report.unresolved[0]
        assert u.kind == "from-import"
        assert "fake_pkg" in u.symbol

    def test_real_world_hallucination_get_current_user_with_roles(self, tmp_path):
        # The hallucination CodeReviewer caught in M1 — the engineer
        # imported get_current_user_with_roles which doesn't exist.
        # In our model: app.core.auth has get_current_user but NOT
        # get_current_user_with_roles. The validator only checks
        # MODULE existence (not symbol existence WITHIN a module).
        # So this test verifies that the *module* check succeeds —
        # but a module-level missing FUNCTION would only fail at
        # runtime when imported. Document the limitation here.
        ws = _ws(tmp_path)
        (ws / "app").mkdir()
        (ws / "app" / "core").mkdir()
        (ws / "app" / "__init__.py").write_text("")
        (ws / "app" / "core" / "__init__.py").write_text("")
        (ws / "app" / "core" / "auth.py").write_text(
            "def get_current_user(): pass\n"
        )
        f = ws / "app" / "api.py"
        f.write_text(
            "from app.core.auth import get_current_user_with_roles\n"
        )
        report = validate_python_file(f, ws)
        # The module resolves (app.core.auth exists), so this passes
        # the AST check. Symbol-level resolution is a deeper check
        # for a future pass.
        assert report.passed  # known limitation

    def test_completely_made_up_module_with_real_looking_name(self, tmp_path):
        ws = _ws(tmp_path)
        f = ws / "x.py"
        f.write_text(
            "from fastapi_extras_pro_max import SuperRouter\n"
        )
        report = validate_python_file(f, ws)
        assert not report.passed
        assert any("fastapi_extras_pro_max" in u.symbol for u in report.unresolved)


# ── Syntax errors ──────────────────────────────────────────────────────


class TestSyntaxErrors:
    def test_syntax_error_caught(self, tmp_path):
        ws = _ws(tmp_path)
        f = ws / "broken.py"
        f.write_text("def foo(:\n    return 1\n")
        report = validate_python_file(f, ws)
        assert not report.passed
        assert report.syntax_errors
        assert "SyntaxError" in report.syntax_errors[0]

    def test_missing_file(self, tmp_path):
        ws = _ws(tmp_path)
        f = ws / "absent.py"
        report = validate_python_file(f, ws)
        assert not report.passed
        assert "file not found" in report.syntax_errors[0]


# ── render ─────────────────────────────────────────────────────────────


class TestRender:
    def test_passed_message(self, tmp_path):
        r = SymbolValidationReport(file_count=2, resolved_count=10)
        out = r.render()
        assert "PASSED" in out
        assert "2 file" in out
        assert "10" in out

    def test_failed_message_lists_unresolved(self, tmp_path):
        ws = _ws(tmp_path)
        f = ws / "x.py"
        f.write_text("import nonexistent_lib\n")
        r = validate_python_file(f, ws)
        out = r.render()
        assert "FAILED" in out
        assert "nonexistent_lib" in out


# ── validate_files (multi) ─────────────────────────────────────────────


class TestValidateFiles:
    def test_multiple_files_aggregated(self, tmp_path):
        ws = _ws(tmp_path)
        a = ws / "a.py"
        a.write_text("import os\n")
        b = ws / "b.py"
        b.write_text("import nonexistent\n")
        report = validate_files([a, b], ws)
        assert report.file_count == 2
        assert len(report.unresolved) == 1
        # Output should mention the bad file but be agnostic to order
        out = report.render()
        assert "nonexistent" in out

    def test_skips_non_python_files(self, tmp_path):
        ws = _ws(tmp_path)
        py = ws / "x.py"
        py.write_text("import os\n")
        ts = ws / "x.ts"
        ts.write_text("import { something } from 'pkg';\n")
        report = validate_files([py, ts], ws)
        assert report.file_count == 1  # only py counted
