"""Python documenter — walks a workspace and extracts classes,
functions, Pydantic models, and route declarations using stdlib
``ast``. No external dependencies.

Output shape (per service workspace):

    {
        "service": "backend",
        "language": "python",
        "framework_hints": ["fastapi"],   # detected from imports
        "files": {
            "app/api/routes/auth.py": {
                "imports": [{"module": "...", "names": ["..."]}],
                "classes": [
                    {
                        "name": "LoginRequest",
                        "bases": ["BaseModel"],
                        "docstring": "...",
                        "fields": [{"name": "email", "type": "EmailStr",
                                    "default": null}],
                        "methods": [...]
                    }
                ],
                "functions": [
                    {
                        "name": "login",
                        "decorators": ["router.post(\"/login\")"],
                        "params": [{"name": "credentials",
                                    "type": "LoginRequest",
                                    "default": null}],
                        "return_type": "LoginResponse",
                        "docstring": "...",
                        "is_async": true
                    }
                ]
            }
        }
    }

Consumers (coder prompt injector, engineer pre-flight, etc.) read
the slice they need.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


# Directory and file names we never descend into.
_SKIP_DIRS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env", ".env",
    "node_modules", ".git", ".idea", ".vscode",
    "dist", "build", "site-packages",
    "tests",  # we don't document test files; engineer doesn't import from tests
}
_SKIP_FILE_PREFIXES = ("test_", "_test")
_SKIP_FILE_SUFFIXES = ("_test.py",)


@dataclass
class PythonAstDocumenter:
    """Walks a Python service workspace and emits a structured contract.

    Parameters
    ----------
    workspace_root:
        The directory containing the service's source code (e.g.
        ``~/bizniz_projects/property_manager_v1/backend``).
    service_name:
        Logical name of the service (``"backend"``). Embedded in the
        output for traceability.
    """

    workspace_root: Path
    service_name: str = ""

    def extract(self) -> Dict[str, Any]:
        files: Dict[str, Dict[str, Any]] = {}
        framework_hints: set[str] = set()

        for py_path in self._iter_python_files():
            rel = str(py_path.relative_to(self.workspace_root))
            try:
                file_doc = self._extract_file(py_path)
            except SyntaxError as e:
                # Don't fail the whole extract on one bad file —
                # surface the error in the doc so consumers know.
                file_doc = {
                    "imports": [],
                    "classes": [],
                    "functions": [],
                    "_parse_error": f"{type(e).__name__}: {e}",
                }
            files[rel] = file_doc
            framework_hints.update(_detect_frameworks(file_doc))

        return {
            "service": self.service_name,
            "language": "python",
            "framework_hints": sorted(framework_hints),
            "files": files,
        }

    def write(self, output_dir: Path) -> Path:
        """Render to ``<output_dir>/api.json`` and return its path."""
        doc = self.extract()
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "api.json"
        out_path.write_text(json.dumps(doc, indent=2, sort_keys=True))
        return out_path

    # ── walk ────────────────────────────────────────────────────────

    def _iter_python_files(self):
        for path in sorted(self.workspace_root.rglob("*.py")):
            # Skip if any segment of the relative path is a skip dir.
            rel_parts = path.relative_to(self.workspace_root).parts
            if any(seg in _SKIP_DIRS for seg in rel_parts[:-1]):
                continue
            name = path.name
            if name.startswith(_SKIP_FILE_PREFIXES):
                continue
            if name.endswith(_SKIP_FILE_SUFFIXES):
                continue
            yield path

    # ── per-file extraction ─────────────────────────────────────────

    def _extract_file(self, path: Path) -> Dict[str, Any]:
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
                classes.append(self._extract_class(node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(self._extract_function(node))

        return {
            "imports": imports,
            "classes": classes,
            "functions": functions,
        }

    def _extract_class(self, node: ast.ClassDef) -> Dict[str, Any]:
        fields: List[Dict[str, Any]] = []
        methods: List[Dict[str, Any]] = []

        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fields.append({
                    "name": item.target.id,
                    "type": _annotation_to_string(item.annotation),
                    "default": _expr_to_string(item.value) if item.value else None,
                })
            elif isinstance(item, ast.Assign):
                # Untyped class attributes (rare in modern Python with type hints,
                # but we pick them up for completeness).
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        fields.append({
                            "name": target.id,
                            "type": None,
                            "default": _expr_to_string(item.value),
                        })
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(self._extract_function(item))

        return {
            "name": node.name,
            "bases": [_annotation_to_string(b) for b in node.bases],
            "decorators": [_annotation_to_string(d) for d in node.decorator_list],
            "docstring": ast.get_docstring(node),
            "fields": fields,
            "methods": methods,
        }

    def _extract_function(self, node) -> Dict[str, Any]:
        params: List[Dict[str, Any]] = []
        args = node.args

        # Compute defaults alignment: the LAST N positional args have defaults.
        all_pos = list(args.posonlyargs) + list(args.args)
        n_defaults = len(args.defaults)
        defaults_for_pos = [None] * (len(all_pos) - n_defaults) + list(args.defaults)

        for arg, default in zip(all_pos, defaults_for_pos):
            params.append({
                "name": arg.arg,
                "type": _annotation_to_string(arg.annotation) if arg.annotation else None,
                "default": _expr_to_string(default) if default else None,
            })

        # *args
        if args.vararg:
            params.append({
                "name": f"*{args.vararg.arg}",
                "type": _annotation_to_string(args.vararg.annotation) if args.vararg.annotation else None,
                "default": None,
            })

        # keyword-only args
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            params.append({
                "name": arg.arg,
                "type": _annotation_to_string(arg.annotation) if arg.annotation else None,
                "default": _expr_to_string(default) if default else None,
                "kw_only": True,
            })

        # **kwargs
        if args.kwarg:
            params.append({
                "name": f"**{args.kwarg.arg}",
                "type": _annotation_to_string(args.kwarg.annotation) if args.kwarg.annotation else None,
                "default": None,
            })

        return {
            "name": node.name,
            "decorators": [_annotation_to_string(d) for d in node.decorator_list],
            "params": params,
            "return_type": _annotation_to_string(node.returns) if node.returns else None,
            "docstring": ast.get_docstring(node),
            "is_async": isinstance(node, ast.AsyncFunctionDef),
        }


# ── helpers ────────────────────────────────────────────────────────


def _annotation_to_string(node: Optional[ast.AST]) -> Optional[str]:
    """Render a type annotation back to source-like text.

    Uses ast.unparse (Python 3.9+) which we rely on elsewhere. Falls
    back to a tag if unparse fails.
    """
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return f"<unparseable:{type(node).__name__}>"


def _expr_to_string(node: Optional[ast.AST]) -> Optional[str]:
    return _annotation_to_string(node)


def _detect_frameworks(file_doc: Dict[str, Any]) -> List[str]:
    """Sniff imports for known framework signatures."""
    hints: List[str] = []
    for imp in file_doc.get("imports", []):
        module = imp.get("module") or ""
        names = imp.get("names") or []
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
