#!/usr/bin/env python3
"""Python documenter sidecar entry point.

Walks ``/workspace`` and emits a structured JSON contract describing
classes, functions, Pydantic models, etc. Same shape as the
TypeScript sidecar's output so consumers (the coder prompt
injector, engineer pre-flight, evolve-mode architect) read uniform
artifacts regardless of source language.

Run with:
    python /opt/extractor/extract_python.py <workspace_path> <service_name>

Output goes to stdout. Stderr reserved for warnings/errors.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


_SKIP_DIRS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env", ".env",
    "node_modules", ".git", ".idea", ".vscode",
    "dist", "build", "site-packages",
    "tests",
}
_SKIP_FILE_PREFIXES = ("test_", "_test")
_SKIP_FILE_SUFFIXES = ("_test.py",)


def iter_python_files(workspace_root: Path):
    for path in sorted(workspace_root.rglob("*.py")):
        rel_parts = path.relative_to(workspace_root).parts
        if any(seg in _SKIP_DIRS for seg in rel_parts[:-1]):
            continue
        name = path.name
        if name.startswith(_SKIP_FILE_PREFIXES):
            continue
        if name.endswith(_SKIP_FILE_SUFFIXES):
            continue
        yield path


def annotation_to_string(node: Optional[ast.AST]) -> Optional[str]:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return f"<unparseable:{type(node).__name__}>"


def expr_to_string(node: Optional[ast.AST]) -> Optional[str]:
    return annotation_to_string(node)


def extract_function(node) -> Dict[str, Any]:
    params: List[Dict[str, Any]] = []
    args = node.args
    all_pos = list(args.posonlyargs) + list(args.args)
    n_defaults = len(args.defaults)
    defaults_for_pos = [None] * (len(all_pos) - n_defaults) + list(args.defaults)

    for arg, default in zip(all_pos, defaults_for_pos):
        params.append({
            "name": arg.arg,
            "type": annotation_to_string(arg.annotation) if arg.annotation else None,
            "default": expr_to_string(default) if default else None,
        })

    if args.vararg:
        params.append({
            "name": f"*{args.vararg.arg}",
            "type": annotation_to_string(args.vararg.annotation) if args.vararg.annotation else None,
            "default": None,
        })

    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        params.append({
            "name": arg.arg,
            "type": annotation_to_string(arg.annotation) if arg.annotation else None,
            "default": expr_to_string(default) if default else None,
            "kw_only": True,
        })

    if args.kwarg:
        params.append({
            "name": f"**{args.kwarg.arg}",
            "type": annotation_to_string(args.kwarg.annotation) if args.kwarg.annotation else None,
            "default": None,
        })

    return {
        "name": node.name,
        "decorators": [annotation_to_string(d) for d in node.decorator_list],
        "params": params,
        "return_type": annotation_to_string(node.returns) if node.returns else None,
        "docstring": ast.get_docstring(node),
        "is_async": isinstance(node, ast.AsyncFunctionDef),
    }


def extract_class(node: ast.ClassDef) -> Dict[str, Any]:
    fields: List[Dict[str, Any]] = []
    methods: List[Dict[str, Any]] = []
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            fields.append({
                "name": item.target.id,
                "type": annotation_to_string(item.annotation),
                "default": expr_to_string(item.value) if item.value else None,
            })
        elif isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    fields.append({
                        "name": target.id,
                        "type": None,
                        "default": expr_to_string(item.value),
                    })
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(extract_function(item))
    return {
        "name": node.name,
        "bases": [annotation_to_string(b) for b in node.bases],
        "decorators": [annotation_to_string(d) for d in node.decorator_list],
        "docstring": ast.get_docstring(node),
        "fields": fields,
        "methods": methods,
    }


def extract_file(path: Path) -> Dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    imports: List[Dict[str, Any]] = []
    classes: List[Dict[str, Any]] = []
    functions: List[Dict[str, Any]] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.append({
                "module": None,
                "names": [alias.name for alias in node.names],
            })
        elif isinstance(node, ast.ImportFrom):
            imports.append({
                "module": node.module,
                "names": [alias.name for alias in node.names],
            })
        elif isinstance(node, ast.ClassDef):
            classes.append(extract_class(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(extract_function(node))

    return {
        "imports": imports,
        "classes": classes,
        "functions": functions,
    }


def detect_frameworks(file_doc: Dict[str, Any]) -> List[str]:
    hints: List[str] = []
    for imp in file_doc.get("imports", []):
        module = imp.get("module") or ""
        if module == "fastapi" or "fastapi" in module.split("."):
            hints.append("fastapi")
        elif module == "flask" or "flask" in module.split("."):
            hints.append("flask")
        elif module == "django" or "django" in module.split("."):
            hints.append("django")
        elif module == "pydantic" or "pydantic" in module.split("."):
            hints.append("pydantic")
        elif module == "sqlalchemy" or "sqlalchemy" in module.split("."):
            hints.append("sqlalchemy")
        elif module == "celery":
            hints.append("celery")
    return hints


def main():
    workspace = Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace")
    service_name = sys.argv[2] if len(sys.argv) > 2 else ""

    files: Dict[str, Dict[str, Any]] = {}
    framework_hints: set[str] = set()

    for py_path in iter_python_files(workspace):
        rel = str(py_path.relative_to(workspace))
        try:
            file_doc = extract_file(py_path)
        except SyntaxError as e:
            file_doc = {
                "imports": [],
                "classes": [],
                "functions": [],
                "_parse_error": f"{type(e).__name__}: {e}",
            }
        files[rel] = file_doc
        framework_hints.update(detect_frameworks(file_doc))

    out = {
        "service": service_name,
        "language": "python",
        "framework_hints": sorted(framework_hints),
        "files": files,
    }
    sys.stdout.write(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
