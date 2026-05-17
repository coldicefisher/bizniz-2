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


# ── Attribute-access validation (v33 lesson: settings.bad_field) ──────


class TestAttributeAccess:
    def _make_config_module(self, ws: Path) -> None:
        """The classic Pydantic Settings + singleton pattern."""
        (ws / "app").mkdir()
        (ws / "app" / "__init__.py").write_text("")
        (ws / "app" / "core").mkdir()
        (ws / "app" / "core" / "__init__.py").write_text("")
        (ws / "app" / "core" / "config.py").write_text(
            "from pydantic_settings import BaseSettings\n"
            "class Settings(BaseSettings):\n"
            "    fusionauth_app_id: str = ''\n"
            "    fusionauth_url: str = ''\n"
            "def get_settings() -> Settings:\n"
            "    return Settings()\n"
            "settings = get_settings()\n"
        )

    def test_flags_bad_attribute_on_imported_singleton(self, tmp_path):
        ws = _ws(tmp_path)
        self._make_config_module(ws)
        bad = ws / "app" / "auth.py"
        bad.write_text(
            "from app.core.config import settings\n"
            "def f():\n"
            "    return settings.fusionauth_application_id\n"
        )
        report = validate_files([bad], ws)
        assert not report.passed
        assert len(report.unresolved_attributes) == 1
        u = report.unresolved_attributes[0]
        assert u.attribute == "fusionauth_application_id"
        assert u.var == "settings"
        assert "fusionauth_app_id" in u.available
        out = report.render()
        assert "fusionauth_application_id" in out
        assert "fusionauth_app_id" in out

    def test_accepts_real_attribute(self, tmp_path):
        ws = _ws(tmp_path)
        self._make_config_module(ws)
        ok = ws / "app" / "auth.py"
        ok.write_text(
            "from app.core.config import settings\n"
            "def f():\n"
            "    return settings.fusionauth_app_id\n"
        )
        report = validate_files([ok], ws)
        assert report.passed

    def test_flags_attribute_on_locally_instantiated_class(self, tmp_path):
        ws = _ws(tmp_path)
        self._make_config_module(ws)
        bad = ws / "app" / "service.py"
        bad.write_text(
            "from app.core.config import Settings\n"
            "s = Settings()\n"
            "x = s.bogus_field\n"
        )
        report = validate_files([bad], ws)
        assert not report.passed
        assert any(
            a.attribute == "bogus_field" and "fusionauth_app_id" in a.available
            for a in report.unresolved_attributes
        )

    def test_typed_local_var_via_get_callable(self, tmp_path):
        ws = _ws(tmp_path)
        self._make_config_module(ws)
        bad = ws / "app" / "route.py"
        bad.write_text(
            "from app.core.config import get_settings\n"
            "def handler():\n"
            "    s = get_settings()\n"
            "    return s.fusionauth_application_id\n"
        )
        report = validate_files([bad], ws)
        assert any(
            a.attribute == "fusionauth_application_id"
            for a in report.unresolved_attributes
        )

    def test_skips_when_class_not_in_workspace(self, tmp_path):
        """External classes (FastAPI Request, Pydantic BaseModel)
        shouldn't be flagged — we can't see their field sets, so we
        say nothing rather than false-positive."""
        ws = _ws(tmp_path)
        bad = ws / "x.py"
        bad.write_text(
            "from fastapi import Request\n"
            "def f(req):\n"
            "    return req\n"  # no resolvable .attr to flag
        )
        report = validate_files([bad], ws)
        assert not report.unresolved_attributes

    def test_flags_class_level_attribute_on_sqlalchemy_model(self, tmp_path):
        """The 2026-05-17 recipe_v2 incident: ``User.user_id`` referenced
        but the User model has ``id`` as PK. Class-level attribute
        access (no instance involved) must be caught."""
        ws = _ws(tmp_path)
        (ws / "app").mkdir()
        (ws / "app" / "__init__.py").write_text("")
        (ws / "app" / "db").mkdir()
        (ws / "app" / "db" / "__init__.py").write_text("")
        (ws / "app" / "db" / "base.py").write_text(
            "class Base: pass\n"
        )
        (ws / "app" / "models").mkdir()
        (ws / "app" / "models" / "__init__.py").write_text("")
        (ws / "app" / "models" / "user.py").write_text(
            "from sqlalchemy.orm import Mapped, mapped_column\n"
            "from sqlalchemy import String\n"
            "from app.db.base import Base\n"
            "class User(Base):\n"
            "    __tablename__ = 'users'\n"
            "    id: Mapped[str] = mapped_column(String, primary_key=True)\n"
            "    email: Mapped[str] = mapped_column(String)\n"
            "    role: Mapped[str] = mapped_column(String)\n"
        )
        (ws / "app" / "core").mkdir()
        (ws / "app" / "core" / "__init__.py").write_text("")
        bad = ws / "app" / "core" / "auth.py"
        bad.write_text(
            "from sqlalchemy import select\n"
            "from app.models.user import User\n"
            "async def get_current_user(db, fa_user_id):\n"
            "    result = await db.execute(\n"
            "        select(User).where(User.user_id == fa_user_id)\n"
            "    )\n"
            "    return result.scalar_one_or_none()\n"
        )
        report = validate_files([bad], ws)
        assert not report.passed, (
            "Expected User.user_id reference to be flagged"
        )
        # The new class-level check should catch this exact pattern.
        flagged = [
            a for a in report.unresolved_attributes
            if a.attribute == "user_id" and a.class_name == "User"
        ]
        assert len(flagged) == 1
        assert "id" in flagged[0].available

    def test_class_level_attribute_with_real_field_passes(self, tmp_path):
        """Sanity: when the class access uses a real column, the
        attribute-level check produces no flag (other validator
        layers may flag missing requirements, but that's separate)."""
        ws = _ws(tmp_path)
        (ws / "app").mkdir()
        (ws / "app" / "__init__.py").write_text("")
        (ws / "app" / "db").mkdir()
        (ws / "app" / "db" / "__init__.py").write_text("")
        (ws / "app" / "db" / "base.py").write_text(
            "class Base: pass\n"
        )
        (ws / "app" / "models").mkdir()
        (ws / "app" / "models" / "__init__.py").write_text("")
        (ws / "app" / "models" / "user.py").write_text(
            "from app.db.base import Base\n"
            "class User(Base):\n"
            "    __tablename__ = 'users'\n"
            "    id: str = ''\n"
        )
        (ws / "app" / "core").mkdir()
        (ws / "app" / "core" / "__init__.py").write_text("")
        ok = ws / "app" / "core" / "auth.py"
        ok.write_text(
            "from app.models.user import User\n"
            "def lookup(x):\n"
            "    return User.id == x\n"
        )
        report = validate_files([ok], ws)
        # We only care that the attribute check didn't flag User.id.
        # Other validators (third-party imports, etc.) aren't relevant
        # here — this test is specifically about the new
        # class-level attribute logic.
        user_id_flags = [
            a for a in report.unresolved_attributes
            if a.class_name == "User"
        ]
        assert user_id_flags == [], (
            f"Expected no User.* attribute flags, got: {user_id_flags}"
        )

    def test_inherited_fields_resolve(self, tmp_path):
        ws = _ws(tmp_path)
        (ws / "models.py").write_text(
            "class Base:\n"
            "    a: int = 0\n"
            "class Child(Base):\n"
            "    b: int = 0\n"
        )
        ok = ws / "use.py"
        ok.write_text(
            "from models import Child\n"
            "c = Child()\n"
            "x = c.a\n"   # inherited
            "y = c.b\n"   # direct
        )
        report = validate_files([ok], ws)
        assert report.passed
