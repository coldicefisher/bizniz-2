"""Tests for the deterministic import verifier (D19 Step 4a)."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from bizniz.refactorer.import_verifier import ImportVerifier


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip("\n"))
    return path


# ── Resolution ──────────────────────────────────────────────────


class TestModuleResolution:
    def test_absolute_import_resolves(self, tmp_path):
        # Project layout: tmp_path is the search root.
        # Module: app/services/recipes.py
        _write(tmp_path / "app" / "__init__.py", "")
        _write(tmp_path / "app" / "services" / "__init__.py", "")
        _write(
            tmp_path / "app" / "services" / "recipes.py",
            """
            def create(): pass
            def list_all(): pass
            """,
        )
        consumer = _write(
            tmp_path / "app" / "api" / "routes" / "recipes.py",
            """
            from app.services.recipes import create, list_all
            def handler():
                create()
                list_all()
            """,
        )
        _write(tmp_path / "app" / "api" / "__init__.py", "")
        _write(tmp_path / "app" / "api" / "routes" / "__init__.py", "")

        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        assert report.passed, report.problems
        assert report.files_checked == 1

    def test_missing_module_flagged(self, tmp_path):
        # Importer references app.services.recipes, but file doesn't exist.
        consumer = _write(
            tmp_path / "app" / "api" / "routes" / "broken.py",
            "from app.services.recipes import create\n",
        )
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        assert not report.passed
        assert len(report.problems) == 1
        p = report.problems[0]
        assert "app.services.recipes" in p.statement
        assert "not found" in p.reason

    def test_module_present_but_symbol_missing_flagged(self, tmp_path):
        _write(tmp_path / "app" / "__init__.py", "")
        _write(tmp_path / "app" / "services" / "__init__.py", "")
        _write(
            tmp_path / "app" / "services" / "recipes.py",
            """
            def create(): pass
            """,
        )
        consumer = _write(
            tmp_path / "app" / "consumer.py",
            "from app.services.recipes import create, list_all\n",
        )
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        assert not report.passed
        # `create` resolves; `list_all` does not.
        bad = [p for p in report.problems if "list_all" in p.statement]
        assert len(bad) == 1

    def test_third_party_imports_not_flagged(self, tmp_path):
        """Imports outside project search roots are assumed OK
        (the test run is the source of truth for those)."""
        consumer = _write(
            tmp_path / "consumer.py",
            """
            import fastapi
            from sqlalchemy.orm import Session
            from pydantic import BaseModel
            """,
        )
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        # None of these are project-local (no app./python_core./ts_core. prefix).
        assert report.passed

    def test_resolves_via_python_core_root(self, tmp_path):
        """Project layout where ``python_core`` is on PYTHONPATH:
        ``from python_core.recipes.pricing import compute`` should
        resolve when ``tmp_path/python_core`` is in search_roots."""
        _write(
            tmp_path / "python_core" / "recipes" / "__init__.py", "",
        )
        _write(
            tmp_path / "python_core" / "recipes" / "pricing.py",
            "def compute(): pass\n",
        )
        _write(tmp_path / "python_core" / "__init__.py", "")
        consumer = _write(
            tmp_path / "backend" / "app" / "api" / "routes" / "recipes.py",
            "from python_core.recipes.pricing import compute\n",
        )
        # Note: search_roots typically contains tmp_path (so 'app.*'
        # resolves) AND tmp_path (so 'python_core.*' resolves via the
        # same root). For this test the same root works for both.
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        assert report.passed, report.problems


# ── Relative imports ────────────────────────────────────────────


class TestRelativeImports:
    def test_one_level_relative_resolves(self, tmp_path):
        _write(tmp_path / "app" / "__init__.py", "")
        _write(tmp_path / "app" / "models" / "__init__.py", "")
        _write(
            tmp_path / "app" / "models" / "user.py",
            "class User: pass\n",
        )
        consumer = _write(
            tmp_path / "app" / "models" / "session.py",
            "from .user import User\n",
        )
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        assert report.passed, report.problems

    def test_two_level_relative_resolves(self, tmp_path):
        _write(tmp_path / "app" / "__init__.py", "")
        _write(tmp_path / "app" / "models" / "__init__.py", "")
        _write(tmp_path / "app" / "api" / "__init__.py", "")
        _write(
            tmp_path / "app" / "models" / "user.py",
            "class User: pass\n",
        )
        consumer = _write(
            tmp_path / "app" / "api" / "routes.py",
            "from ..models.user import User\n",
        )
        _write(tmp_path / "app" / "api" / "__init__.py", "")
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        assert report.passed, report.problems

    def test_relative_escaping_package_is_flagged(self, tmp_path):
        consumer = _write(
            tmp_path / "app" / "deeply.py",
            "from ...too_far import x\n",
        )
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        assert not report.passed
        assert "outside any package root" in report.problems[0].reason


# ── Edge cases ──────────────────────────────────────────────────


class TestEdgeCases:
    def test_syntax_error_flagged(self, tmp_path):
        consumer = _write(
            tmp_path / "broken.py",
            "def foo(\n",  # incomplete
        )
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        assert not report.passed
        assert "SyntaxError" in report.problems[0].reason

    def test_missing_file_skipped(self, tmp_path):
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([tmp_path / "does-not-exist.py"])
        assert report.files_checked == 0
        assert report.passed

    def test_wildcard_imports_not_validated(self, tmp_path):
        _write(tmp_path / "app" / "__init__.py", "")
        _write(
            tmp_path / "app" / "utils.py",
            "def foo(): pass\n",
        )
        consumer = _write(
            tmp_path / "app" / "consumer.py",
            "from app.utils import *\n",
        )
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        assert report.passed

    def test_submodule_import_in_from(self, tmp_path):
        """from app import services — services is a submodule
        (package), not a name defined in app/__init__.py. Verifier
        should accept this."""
        _write(tmp_path / "app" / "__init__.py", "")
        _write(tmp_path / "app" / "services" / "__init__.py", "")
        consumer = _write(
            tmp_path / "consumer.py",
            "from app import services\n",
        )
        report = ImportVerifier(
            search_roots=[tmp_path],
        ).verify_files([consumer])
        assert report.passed, report.problems
