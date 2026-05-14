"""Deterministic discovery tools — AST/structure-based introspection.

The agent's go-to lookup tools. All deterministic, all token-cheap,
all 100% reliable. Encourage the agent in its system prompt to use
these BEFORE reaching for ``view_file`` or ``search_files``.

Tools:
  - ``search_imports(symbol)``        find a symbol's definition + signature
  - ``list_all_imports(module)``      every importable name in a module
  - ``get_file_outline(path)``        classes / functions / imports only
  - ``get_workspace_tree()``          pre-filtered tree of the service workspace
  - ``list_routes(service)``          HTTP routes via FastAPI/React-router AST
  - ``list_dependencies()``           parsed requirements.txt / package.json
  - ``list_pydantic_models()``        every BaseModel + its fields

All are read-only. None mutate state. None hit the network or
containers.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from bizniz.tools.import_tools import (
    build_workspace_index,
    list_all_imports as _v1_list_all_imports,
    search_imports as _v1_search_imports,
)
from bizniz.workspace.base_workspace import BaseWorkspace


ToolHandler = Callable[[Dict], str]


_MAX_RESULT_BYTES = 64 * 1024


def _truncate(s: str, n: int = _MAX_RESULT_BYTES) -> str:
    return s if len(s) <= n else s[:n] + f"\n\n... (truncated, total {len(s)} bytes)"


# ── search_imports / list_all_imports (port v1's AST plumbing) ────────


def make_search_imports(workspace: BaseWorkspace) -> ToolHandler:
    """Find where a symbol is defined. Returns the signature, docstring,
    and module path. Fuzzy-matches on miss.

    Action takes the symbol name in the ``path`` field (compatibility
    with the universal action schema)."""
    def handler(action: Dict) -> str:
        symbol = (action.get("path") or "").strip()
        if not symbol:
            return "ERROR: search_imports requires a symbol name in 'path'."
        try:
            root = Path(workspace.root)
            index = build_workspace_index(root)
            return _truncate(_v1_search_imports(symbol, index))
        except Exception as e:
            return f"ERROR: search_imports failed: {type(e).__name__}: {e}"
    return handler


def make_list_all_imports(workspace: BaseWorkspace) -> ToolHandler:
    """List every importable symbol in a module with full signatures.

    Action takes the module path in the ``path`` field
    (e.g. ``app.core.auth``)."""
    def handler(action: Dict) -> str:
        module_path = (action.get("path") or "").strip()
        if not module_path:
            return "ERROR: list_all_imports requires a module path in 'path'."
        try:
            root = Path(workspace.root)
            index = build_workspace_index(root)
            return _truncate(_v1_list_all_imports(module_path, index))
        except Exception as e:
            return f"ERROR: list_all_imports failed: {type(e).__name__}: {e}"
    return handler


# ── get_file_outline ──────────────────────────────────────────────────


def _python_outline(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"(SyntaxError on parse: {e.msg} at line {e.lineno})"

    lines: List[str] = []

    # Top-level imports
    imports = [
        node for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    if imports:
        lines.append("# Imports")
        for node in imports:
            try:
                lines.append(f"  L{node.lineno}: {ast.unparse(node)}")
            except Exception:
                pass
        lines.append("")

    def _summarize_func(node, indent: str = ""):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        try:
            args = ast.unparse(node.args)
        except Exception:
            args = "..."
        ret = ""
        if node.returns:
            try:
                ret = f" -> {ast.unparse(node.returns)}"
            except Exception:
                ret = ""
        sig = f"{prefix} {node.name}({args}){ret}"
        ds = ast.get_docstring(node) or ""
        first = ds.splitlines()[0] if ds else ""
        line = f"{indent}L{node.lineno}: {sig}"
        if first:
            line += f"  # {first[:80]}"
        lines.append(line)

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ""
            if node.bases:
                try:
                    bases = "(" + ", ".join(ast.unparse(b) for b in node.bases) + ")"
                except Exception:
                    bases = ""
            ds = ast.get_docstring(node) or ""
            first = ds.splitlines()[0] if ds else ""
            head = f"L{node.lineno}: class {node.name}{bases}:"
            if first:
                head += f"  # {first[:80]}"
            lines.append(head)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _summarize_func(sub, indent="    ")
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    try:
                        ann = ast.unparse(sub.annotation)
                    except Exception:
                        ann = "?"
                    lines.append(f"    L{sub.lineno}: {sub.target.id}: {ann}")
            lines.append("")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _summarize_func(node)

    if not lines:
        return "(no top-level classes, functions, or imports)"
    return "\n".join(lines)


def _typescript_outline(source: str) -> str:
    """Cheap regex-based outline for TS/TSX. Not as precise as ast for
    Python but good enough for navigation."""
    out: List[str] = []
    import_re = re.compile(r"^\s*(import\s.+?from\s+['\"][^'\"]+['\"];?)", re.MULTILINE)
    for m in import_re.finditer(source):
        line = source[: m.start()].count("\n") + 1
        out.append(f"# Import — L{line}: {m.group(1).strip()}")

    decl_re = re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?"
        r"(class|function|const|interface|type|enum)\s+(\w+)",
        re.MULTILINE,
    )
    for m in decl_re.finditer(source):
        line = source[: m.start()].count("\n") + 1
        out.append(f"L{line}: {m.group(1)} {m.group(2)}")

    return "\n".join(out) if out else "(no top-level declarations)"


def make_get_file_outline(workspace: BaseWorkspace) -> ToolHandler:
    """Return classes + functions + imports of a file, no bodies.

    Saves typically 80% tokens vs ``view_file`` for files >100 LOC.
    Use this when navigating; only fall back to ``view_file`` when you
    need an actual function body."""
    def handler(action: Dict) -> str:
        path = (action.get("path") or "").strip()
        if not path:
            return "ERROR: get_file_outline requires a 'path'."
        try:
            full = workspace.path(path)
        except Exception as e:
            return f"ERROR: cannot resolve '{path}': {e}"
        if not full.is_file():
            return f"ERROR: '{path}' is not a file."
        try:
            source = full.read_text(errors="replace")
        except Exception as e:
            return f"ERROR: read failed: {e}"

        suffix = full.suffix.lower()
        if suffix == ".py":
            outline = _python_outline(source)
        elif suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            outline = _typescript_outline(source)
        else:
            return (
                f"ERROR: get_file_outline supports .py and .ts/.tsx/.js. "
                f"For other types use view_file."
            )
        return _truncate(f"=== {path} (outline) ===\n{outline}")
    return handler


# ── get_workspace_tree ────────────────────────────────────────────────


def make_get_workspace_tree(workspace: BaseWorkspace) -> ToolHandler:
    """Pre-filtered file tree of the workspace.

    Excludes the usual noise (__pycache__, node_modules, .pytest_cache,
    .venv, etc.). Returns a flat list of all relevant files with sizes,
    capped at 200 entries.
    """
    EXCLUDE_DIRS = {
        "__pycache__", "node_modules", ".pytest_cache", ".venv",
        ".git", ".angular", ".next", "dist", "build", ".cache",
        ".turbo", ".parcel-cache", ".nuxt", ".astro", ".svelte-kit",
    }
    MAX_ENTRIES = 200

    def handler(action: Dict) -> str:
        root = Path(workspace.root) if hasattr(workspace, "root") else None
        if root is None or not root.is_dir():
            return "ERROR: workspace root unavailable."

        entries: List[Tuple[str, int]] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(seg in EXCLUDE_DIRS for seg in path.parts):
                continue
            try:
                size = path.stat().st_size
            except Exception:
                continue
            entries.append((str(path.relative_to(root)), size))
            if len(entries) >= MAX_ENTRIES:
                break

        entries.sort()
        if not entries:
            return "(empty workspace)"
        lines = [f"=== workspace tree ({len(entries)} files) ==="]
        for rel, size in entries:
            lines.append(f"  {rel}  ({size}B)")
        if len(entries) >= MAX_ENTRIES:
            lines.append(f"\n... ({MAX_ENTRIES}+ files; use list_directory for deeper exploration)")
        return _truncate("\n".join(lines))
    return handler


# ── list_routes (Python/FastAPI for now; extend per language later) ───


def _python_list_routes(workspace: BaseWorkspace) -> List[Tuple[str, str, str, str]]:
    """Extract FastAPI routes from a Python workspace.

    Returns list of (method, path, handler_name, file_relpath).
    Walks every .py file under the workspace, looks for decorators
    matching ``@<router>.<method>("path", ...)`` or ``@app.<method>(...)``.
    """
    root = Path(workspace.root) if hasattr(workspace, "root") else None
    if root is None or not root.is_dir():
        return []

    EXCLUDE = {"__pycache__", "node_modules", ".pytest_cache", ".venv", ".git"}
    METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}

    routes: List[Tuple[str, str, str, str]] = []
    for py in root.rglob("*.py"):
        if any(seg in EXCLUDE for seg in py.parts):
            continue
        try:
            tree = ast.parse(py.read_text(errors="replace"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                # We're looking for ``X.method(...)`` where method is in METHODS.
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr.lower() not in METHODS:
                    continue
                if not dec.args:
                    continue
                first = dec.args[0]
                path_str = None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    path_str = first.value
                if path_str is None:
                    continue
                try:
                    rel = py.relative_to(root)
                except Exception:
                    rel = py
                routes.append((
                    func.attr.upper(),
                    path_str,
                    node.name,
                    str(rel),
                ))
    return routes


def make_list_routes(workspace: BaseWorkspace) -> ToolHandler:
    """List HTTP routes the service exposes. Currently supports
    FastAPI (Python). React/TypeScript route enumeration TBD."""
    def handler(action: Dict) -> str:
        try:
            routes = _python_list_routes(workspace)
        except Exception as e:
            return f"ERROR: list_routes failed: {type(e).__name__}: {e}"
        if not routes:
            return (
                "(no routes detected — workspace has no Python files with "
                "FastAPI-style decorators, or this isn't a FastAPI service)"
            )
        lines = [f"=== {len(routes)} route(s) ==="]
        for method, path, handler_name, file in sorted(routes, key=lambda r: (r[1], r[0])):
            lines.append(f"  {method:6s} {path:40s}  {handler_name}  ({file})")
        return _truncate("\n".join(lines))
    return handler


# ── list_dependencies ─────────────────────────────────────────────────


def make_list_dependencies(workspace: BaseWorkspace) -> ToolHandler:
    """Parse declared dependencies. Looks for requirements.txt,
    pyproject.toml [project].dependencies, or package.json dependencies.

    Returns name + (version constraint or "any")."""
    def handler(action: Dict) -> str:
        root = Path(workspace.root) if hasattr(workspace, "root") else None
        if root is None or not root.is_dir():
            return "ERROR: workspace root unavailable."

        out: List[str] = []

        # requirements.txt
        req = root / "requirements.txt"
        if req.is_file():
            out.append(f"=== requirements.txt ({req.relative_to(root)}) ===")
            for line in req.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                out.append(f"  {line}")
            out.append("")

        # pyproject.toml
        pyproj = root / "pyproject.toml"
        if pyproj.is_file():
            text = pyproj.read_text(errors="replace")
            m = re.search(
                r"^\s*dependencies\s*=\s*\[(.*?)\]",
                text,
                re.MULTILINE | re.DOTALL,
            )
            if m:
                out.append(f"=== pyproject.toml [dependencies] ===")
                items = re.findall(r'"([^"]+)"', m.group(1))
                for it in items:
                    out.append(f"  {it}")
                out.append("")

        # package.json
        pkg = root / "package.json"
        if pkg.is_file():
            try:
                data = json.loads(pkg.read_text(errors="replace"))
            except Exception:
                data = {}
            for key in ("dependencies", "devDependencies"):
                deps = data.get(key) or {}
                if not deps:
                    continue
                out.append(f"=== package.json [{key}] ===")
                for name, ver in sorted(deps.items()):
                    out.append(f"  {name}@{ver}")
                out.append("")

        if not out:
            return (
                "(no dependency manifests found — workspace has no "
                "requirements.txt, pyproject.toml, or package.json)"
            )
        return _truncate("\n".join(out))
    return handler


# ── list_pydantic_models ──────────────────────────────────────────────


def make_list_pydantic_models(workspace: BaseWorkspace) -> ToolHandler:
    """Enumerate every Pydantic BaseModel + its fields. Walks .py files
    under the workspace, extracts class definitions inheriting from
    BaseModel (or a subclass thereof, by name), reports field names
    + type annotations + defaults."""
    def handler(action: Dict) -> str:
        root = Path(workspace.root) if hasattr(workspace, "root") else None
        if root is None or not root.is_dir():
            return "ERROR: workspace root unavailable."

        EXCLUDE = {"__pycache__", "node_modules", ".pytest_cache", ".venv", ".git"}
        models: List[str] = []

        for py in root.rglob("*.py"):
            if any(seg in EXCLUDE for seg in py.parts):
                continue
            try:
                tree = ast.parse(py.read_text(errors="replace"))
            except Exception:
                continue
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                # Heuristic: any base named BaseModel or ending in Model
                # (catches `class X(SomeBase)` where SomeBase inherits BaseModel).
                base_names = []
                for b in node.bases:
                    if isinstance(b, ast.Name):
                        base_names.append(b.id)
                    elif isinstance(b, ast.Attribute):
                        base_names.append(b.attr)
                if not any(
                    n == "BaseModel" or n.endswith("Model") or n == "Schema"
                    for n in base_names
                ):
                    continue
                fields = []
                for sub in node.body:
                    if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                        try:
                            ann = ast.unparse(sub.annotation)
                        except Exception:
                            ann = "?"
                        default = ""
                        if sub.value is not None:
                            try:
                                default = f" = {ast.unparse(sub.value)}"
                            except Exception:
                                default = " = ..."
                        fields.append(f"{sub.target.id}: {ann}{default}")
                try:
                    rel = py.relative_to(root)
                except Exception:
                    rel = py
                bases_str = f"({', '.join(base_names)})" if base_names else ""
                head = f"class {node.name}{bases_str}  ({rel}:L{node.lineno})"
                if fields:
                    models.append(head + "\n  " + "\n  ".join(fields))
                else:
                    models.append(head + "\n  (no annotated fields)")

        if not models:
            return "(no Pydantic-shaped models found in workspace)"
        return _truncate(
            f"=== {len(models)} Pydantic-shaped model(s) ===\n\n"
            + "\n\n".join(models)
        )
    return handler


# ── Convenience builder ───────────────────────────────────────────────


def build_discovery_handlers(workspace: BaseWorkspace) -> Dict[str, ToolHandler]:
    """Standard discovery toolkit. Compose into the agent's
    ``tool_handlers()`` dict."""
    return {
        "search_imports": make_search_imports(workspace),
        "list_all_imports": make_list_all_imports(workspace),
        "get_file_outline": make_get_file_outline(workspace),
        "get_workspace_tree": make_get_workspace_tree(workspace),
        "list_routes": make_list_routes(workspace),
        "list_dependencies": make_list_dependencies(workspace),
        "list_pydantic_models": make_list_pydantic_models(workspace),
    }
