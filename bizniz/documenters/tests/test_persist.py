"""Tests for write_service_docs persistence."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bizniz.documenters.persist import write_service_docs, docs_dir_for


def _write(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _make_service(name: str, language: str, framework: str = "fastapi"):
    return SimpleNamespace(
        name=name,
        language=language,
        framework=framework,
        service_type="backend",
        workspace_name=name,
    )


def test_python_docs_persist_to_disk(tmp_path):
    project_root = tmp_path / "project"
    workspace_root = project_root / "backend"
    _write(workspace_root, "app/main.py", '''
from pydantic import BaseModel

class Foo(BaseModel):
    x: int
    y: str
''')

    service = _make_service("backend", "python", "fastapi")
    out = write_service_docs(
        service=service,
        workspace_root=workspace_root,
        project_root=project_root,
    )

    assert out is not None
    expected = project_root / "docs" / "backend" / "code" / "api.json"
    assert out == expected
    assert expected.exists()

    parsed = json.loads(expected.read_text())
    assert parsed["service"] == "backend"
    assert "app/main.py" in parsed["files"]


def test_meta_sidecar_written(tmp_path):
    project_root = tmp_path / "project"
    workspace_root = project_root / "backend"
    _write(workspace_root, "app/main.py", "x = 1\n")

    service = _make_service("backend", "python")
    write_service_docs(
        service=service,
        workspace_root=workspace_root,
        project_root=project_root,
    )

    meta_path = project_root / "docs" / "backend" / "code" / "extract_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["service"] == "backend"
    assert meta["language"] == "python"
    assert meta["extractor"] == "PythonAstDocumenter"
    assert "extracted_at" in meta


def test_unknown_language_skipped(tmp_path):
    """Service with no documenter (e.g. 'csharp' before Phase 5)
    soft-fails — caller's not blocked."""
    project_root = tmp_path / "project"
    workspace_root = project_root / "api"
    _write(workspace_root, "Program.cs", "// stub\n")

    service = _make_service("api", "csharp", "aspnet")
    out = write_service_docs(
        service=service,
        workspace_root=workspace_root,
        project_root=project_root,
    )

    assert out is None
    # No docs dir created because nothing to write.
    assert not (project_root / "docs" / "api" / "code" / "api.json").exists()


def test_docs_dir_creates_path(tmp_path):
    out = docs_dir_for(tmp_path, "frontend")
    assert out == tmp_path / "docs" / "frontend" / "code"
    assert out.is_dir()


@pytest.mark.functional
def test_typescript_docs_persist(tmp_path):
    """Functional: hits the bizniz-doc-typescript sidecar."""
    project_root = tmp_path / "project"
    workspace_root = project_root / "frontend"
    _write(workspace_root, "package.json", '{"name": "test"}')
    _write(workspace_root, "tsconfig.json", '{"compilerOptions": {"jsx": "react-jsx"}}')
    _write(workspace_root, "src/api.ts", "export const x: number = 1;\n")

    service = _make_service("frontend", "typescript", "react")
    out = write_service_docs(
        service=service,
        workspace_root=workspace_root,
        project_root=project_root,
    )

    assert out is not None
    parsed = json.loads(out.read_text())
    assert parsed["service"] == "frontend"
    assert "src/api.ts" in parsed["files"]
