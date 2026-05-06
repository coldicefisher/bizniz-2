"""Tests for the discovery tool factories."""
import pytest

from bizniz.lib.tools.discovery import (
    build_discovery_handlers,
    make_get_file_outline,
    make_get_workspace_tree,
    make_list_dependencies,
    make_list_pydantic_models,
    make_list_routes,
)
from bizniz.workspace.local_workspace import LocalWorkspace


@pytest.fixture
def ws(tmp_path):
    return LocalWorkspace(root=tmp_path)


class TestGetFileOutline:
    def test_python_classes_and_functions(self, ws, tmp_path):
        (tmp_path / "x.py").write_text(
            'import os\n'
            'from typing import List\n'
            '\n'
            'class Foo:\n'
            '    """Foo does things."""\n'
            '    def bar(self, n: int) -> str:\n'
            '        """Returns a string."""\n'
            '        return str(n)\n'
            '\n'
            'def top_level(x: int) -> int:\n'
            '    return x + 1\n'
        )
        handler = make_get_file_outline(ws)
        out = handler({"path": "x.py"})
        assert "class Foo" in out
        assert "def bar" in out
        assert "def top_level" in out
        # Function bodies are NOT included
        assert "return str(n)" not in out
        # Imports are listed
        assert "import os" in out

    def test_python_syntax_error(self, ws, tmp_path):
        (tmp_path / "broken.py").write_text("def x(:\n")
        handler = make_get_file_outline(ws)
        out = handler({"path": "broken.py"})
        assert "SyntaxError" in out

    def test_typescript_outline(self, ws, tmp_path):
        (tmp_path / "x.tsx").write_text(
            "import React from 'react';\n"
            "import { useState } from 'react';\n"
            "\n"
            "export const HomePage = () => {\n"
            "  return <div>Hi</div>;\n"
            "};\n"
            "\n"
            "interface User { id: string }\n"
        )
        handler = make_get_file_outline(ws)
        out = handler({"path": "x.tsx"})
        assert "Import" in out
        assert "react" in out
        assert "const HomePage" in out
        assert "interface User" in out

    def test_unsupported_extension(self, ws, tmp_path):
        (tmp_path / "foo.go").write_text("package main\n")
        handler = make_get_file_outline(ws)
        out = handler({"path": "foo.go"})
        assert "ERROR" in out


class TestGetWorkspaceTree:
    def test_filters_noise_dirs(self, ws, tmp_path):
        (tmp_path / "real.py").write_text("")
        (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
        (tmp_path / "node_modules" / "pkg" / "index.js").write_text("")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "x.pyc").write_text("")
        handler = make_get_workspace_tree(ws)
        out = handler({})
        assert "real.py" in out
        assert "node_modules" not in out
        assert "pycache" not in out.lower() or "__pycache__" not in out


class TestListRoutes:
    def test_finds_fastapi_routes(self, ws, tmp_path):
        (tmp_path / "main.py").write_text(
            'from fastapi import APIRouter\n'
            'router = APIRouter()\n'
            '\n'
            '@router.get("/users")\n'
            'async def list_users():\n'
            '    return []\n'
            '\n'
            '@router.post("/users")\n'
            'async def create_user():\n'
            '    return {}\n'
        )
        handler = make_list_routes(ws)
        out = handler({})
        assert "GET" in out
        assert "POST" in out
        assert "/users" in out
        assert "list_users" in out
        assert "create_user" in out

    def test_no_routes_in_pure_workspace(self, ws, tmp_path):
        (tmp_path / "x.py").write_text("def foo(): pass\n")
        handler = make_list_routes(ws)
        out = handler({})
        assert "no routes" in out.lower()


class TestListDependencies:
    def test_requirements_txt(self, ws, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "fastapi==0.109.0\n"
            "# comment\n"
            "uvicorn>=0.27.0\n"
            "pydantic\n"
        )
        handler = make_list_dependencies(ws)
        out = handler({})
        assert "fastapi==0.109.0" in out
        assert "uvicorn>=0.27.0" in out
        assert "pydantic" in out
        assert "comment" not in out

    def test_package_json(self, ws, tmp_path):
        (tmp_path / "package.json").write_text(
            '{\n'
            '  "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0"},\n'
            '  "devDependencies": {"vite": "^5.0.0"}\n'
            '}\n'
        )
        handler = make_list_dependencies(ws)
        out = handler({})
        assert "react@^18.2.0" in out
        assert "vite@^5.0.0" in out
        assert "[dependencies]" in out
        assert "[devDependencies]" in out

    def test_no_manifests(self, ws):
        handler = make_list_dependencies(ws)
        out = handler({})
        assert "no dependency manifests" in out.lower()


class TestListPydanticModels:
    def test_finds_basemodels(self, ws, tmp_path):
        (tmp_path / "schemas.py").write_text(
            'from pydantic import BaseModel\n'
            'from typing import Optional\n'
            '\n'
            'class User(BaseModel):\n'
            '    name: str\n'
            '    age: int = 0\n'
            '    email: Optional[str] = None\n'
            '\n'
            'class Address(BaseModel):\n'
            '    street: str\n'
        )
        handler = make_list_pydantic_models(ws)
        out = handler({})
        assert "class User" in out
        assert "class Address" in out
        assert "name: str" in out
        assert "age: int = 0" in out

    def test_no_models(self, ws, tmp_path):
        (tmp_path / "x.py").write_text("def foo(): pass\n")
        handler = make_list_pydantic_models(ws)
        out = handler({})
        assert "no Pydantic" in out


class TestBuilder:
    def test_build_returns_all_handlers(self, ws):
        handlers = build_discovery_handlers(ws)
        expected = {
            "search_imports", "list_all_imports", "get_file_outline",
            "get_workspace_tree", "list_routes", "list_dependencies",
            "list_pydantic_models",
        }
        assert set(handlers.keys()) == expected
