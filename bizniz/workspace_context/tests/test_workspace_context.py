"""Tests for WorkspaceContextBuilder + render."""
from __future__ import annotations

import pytest

from bizniz.coder.types import Issue
from bizniz.workspace.local_workspace import LocalWorkspace
from bizniz.workspace_context.aliases import (
    npm_import_for, python_import_for,
)
from bizniz.workspace_context.builder import WorkspaceContextBuilder
from bizniz.workspace_context.types import DeclaredPackage, WorkspaceContext


def _ws(tmp_path) -> LocalWorkspace:
    root = tmp_path / "ws"
    root.mkdir()
    return LocalWorkspace(root)


def _issue(target=None, test=None) -> Issue:
    return Issue(
        id="X", title="t", description="d", service="backend",
        language="python",
        target_files=target or ["app/x.py"],
        test_files=test or ["tests/test_x.py"],
        success_criteria=[], spec_refs=[], depends_on=[],
    )


# ── Alias map ──────────────────────────────────────────────────────


class TestAliases:
    def test_pyjwt_imports_as_jwt(self):
        assert python_import_for("pyjwt") == "jwt"

    def test_python_jose_imports_as_jose(self):
        assert python_import_for("python-jose") == "jose"
        # Handle the underscored variant common in normalization.
        assert python_import_for("python_jose") == "jose"

    def test_pillow_imports_as_pil(self):
        assert python_import_for("pillow") == "PIL"

    def test_unknown_package_returns_itself(self):
        assert python_import_for("some-random-pkg") == "some-random-pkg"

    def test_already_aligned_name_unchanged(self):
        # fastapi, pydantic, sqlalchemy — distribution name == import name
        assert python_import_for("fastapi") == "fastapi"

    def test_npm_import_default_identity(self):
        assert npm_import_for("react") == "react"
        assert npm_import_for("@scope/pkg") == "@scope/pkg"


# ── File reading ──────────────────────────────────────────────────


class TestFileReading:
    def test_existing_files_captured(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("app/x.py", "def me(): return 1\n")
        ws.write_file("tests/test_x.py", "def test_me(): pass\n")
        builder = WorkspaceContextBuilder(workspace=ws)
        ctx = builder.build(_issue())
        assert ctx.target_files_content["app/x.py"] == "def me(): return 1\n"
        assert ctx.test_files_content["tests/test_x.py"] == "def test_me(): pass\n"
        assert ctx.missing_paths == []

    def test_missing_files_recorded(self, tmp_path):
        ws = _ws(tmp_path)
        # nothing written
        builder = WorkspaceContextBuilder(workspace=ws)
        ctx = builder.build(_issue())
        assert ctx.target_files_content == {}
        assert ctx.test_files_content == {}
        assert sorted(ctx.missing_paths) == ["app/x.py", "tests/test_x.py"]

    def test_mixed_existing_and_missing(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("app/x.py", "x\n")
        # tests/test_x.py missing
        builder = WorkspaceContextBuilder(workspace=ws)
        ctx = builder.build(_issue())
        assert "app/x.py" in ctx.target_files_content
        assert "tests/test_x.py" in ctx.missing_paths


# ── Dep parsing ───────────────────────────────────────────────────


class TestDepParsing:
    def test_requirements_txt_parsed(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file(
            "requirements.txt",
            "# comment\n"
            "fastapi==0.115.6\n"
            "pyjwt[crypto]==2.10.0\n"
            "python-jose[cryptography]==3.3.0\n"
            "httpx>=0.28\n"
            "\n"
            "-e ./local-package\n"  # skipped (dash)
        )
        builder = WorkspaceContextBuilder(workspace=ws)
        ctx = builder.build(_issue())
        names = {p.name for p in ctx.declared_python_packages}
        assert "fastapi" in names
        assert "pyjwt" in names
        assert "python-jose" in names
        assert "httpx" in names
        # Import names correctly mapped.
        by_name = {p.name: p for p in ctx.declared_python_packages}
        assert by_name["pyjwt"].import_name == "jwt"
        assert by_name["python-jose"].import_name == "jose"
        assert by_name["fastapi"].import_name == "fastapi"

    def test_package_json_parsed(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file(
            "package.json",
            '{"dependencies": {"react": "^18", "jose": "^5"}, '
            ' "devDependencies": {"vitest": "^1"}}'
        )
        builder = WorkspaceContextBuilder(workspace=ws)
        ctx = builder.build(_issue())
        names = {p.name for p in ctx.declared_node_packages}
        assert names == {"react", "jose", "vitest"}

    def test_no_manifests_returns_empty(self, tmp_path):
        ws = _ws(tmp_path)
        builder = WorkspaceContextBuilder(workspace=ws)
        ctx = builder.build(_issue())
        assert ctx.declared_python_packages == []
        assert ctx.declared_node_packages == []


# ── Prompt rendering ──────────────────────────────────────────────


class TestRender:
    def test_renders_installed_packages_table(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("requirements.txt", "pyjwt==2.10.0\nfastapi==0.115\n")
        builder = WorkspaceContextBuilder(workspace=ws)
        ctx = builder.build(_issue())
        out = ctx.to_prompt_section()
        # Table headers + key entries.
        assert "Installed Python packages" in out
        assert "pyjwt" in out
        assert "fastapi" in out
        assert "`jwt`" in out  # import-name shows as `jwt` for pyjwt
        # Adding-deps instructions.
        assert "Adding new dependencies" in out
        assert "requested_deps" in out
        # Telling agent NOT to test after dep add.
        assert "run pytest to verify" in out or "DO NOT" in out.upper() or "do NOT" in out

    def test_renders_live_file_content(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("app/x.py", "def existing(): return 1\n")
        builder = WorkspaceContextBuilder(workspace=ws)
        ctx = builder.build(_issue())
        out = ctx.to_prompt_section()
        assert "app/x.py" in out
        assert "def existing()" in out

    def test_renders_missing_paths_with_create_hint(self, tmp_path):
        ws = _ws(tmp_path)
        builder = WorkspaceContextBuilder(workspace=ws)
        ctx = builder.build(_issue())
        out = ctx.to_prompt_section()
        assert "don't exist on disk yet" in out or "CREATE them" in out
        assert "app/x.py" in out

    def test_handles_missing_dep_files_gracefully(self, tmp_path):
        # No requirements.txt, no package.json — render still works.
        ws = _ws(tmp_path)
        builder = WorkspaceContextBuilder(workspace=ws)
        ctx = builder.build(_issue())
        out = ctx.to_prompt_section()
        assert out  # non-empty
        # No tables shown when no deps.
        assert "Installed Python packages" not in out
