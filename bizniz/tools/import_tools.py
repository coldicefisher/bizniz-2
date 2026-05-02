"""Import resolution tools for AI agents.

Provides three capabilities:

1. **resolve_import(module_path)** — check if an import resolves in
   the workspace, and if not, suggest the closest matches with full
   signatures.

2. **search_imports(symbol_name)** — find all workspace files that
   export a given symbol, returning full signatures + docstrings.

3. **list_all_imports(module_path)** — list every importable symbol
   in a module with signatures.

These tools are language-specific. Python support is built-in;
TypeScript is a future extension.

Usage by agents:
    from bizniz.tools.import_tools import (
        resolve_import,
        search_imports,
        list_all_imports,
        build_workspace_index,
    )
    index = build_workspace_index(workspace_root)

    # "What can I import from app.core.auth?"
    list_all_imports("app.core.auth", index)

    # "Where is get_current_user defined?"
    search_imports("get_current_user", index)

    # "from app.api.deps import X" — does this resolve?
    resolve_import("app.api.deps", index)
"""
from __future__ import annotations

import ast
import inspect
import os
import textwrap
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set


# ── Data types ───────────────────────────────────────────────────────────────


@dataclass
class ExportedSymbol:
    """A symbol exported by a workspace module."""
    name: str           # e.g. "get_current_user"
    module_path: str    # e.g. "app.core.auth"
    filepath: str       # e.g. "app/core/auth.py"
    kind: str           # "function", "class", "variable", "import"
    signature: str = "" # e.g. "(credentials, db) -> User"
    docstring: str = "" # first paragraph of the docstring
    # For classes: list of method signatures
    methods: List[str] = field(default_factory=list)
    # For classes: list of class-level attribute names
    attributes: List[str] = field(default_factory=list)


@dataclass
class WorkspaceIndex:
    """Index of all importable modules and symbols in a workspace."""
    modules: Dict[str, str] = field(default_factory=dict)
    symbols: Dict[str, List[ExportedSymbol]] = field(default_factory=dict)
    all_module_paths: List[str] = field(default_factory=list)


@dataclass
class ImportResolution:
    """Result of trying to resolve an import."""
    module_path: str
    resolved: bool
    filepath: Optional[str] = None
    suggestions: List[str] = field(default_factory=list)
    available_symbols: List[str] = field(default_factory=list)


# ── Public API ───────────────────────────────────────────────────────────────


def build_workspace_index(workspace_root: Path) -> WorkspaceIndex:
    """Walk the workspace and index all Python modules + exported symbols."""
    index = WorkspaceIndex()

    for dirpath, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {
                "node_modules", "__pycache__", ".pytest_cache", ".git",
                ".bizniz", ".venv", "venv", ".egg-info", "dist", "build",
            }
        ]

        for fname in filenames:
            if not fname.endswith(".py"):
                continue

            filepath = Path(dirpath) / fname
            rel_path = str(filepath.relative_to(workspace_root))
            module_path = _filepath_to_module(rel_path)
            if module_path is None:
                continue

            index.modules[module_path] = rel_path
            index.all_module_paths.append(module_path)

            try:
                content = filepath.read_text(errors="replace")
                symbols = _extract_exports(content, module_path, rel_path)
                for sym in symbols:
                    if sym.name not in index.symbols:
                        index.symbols[sym.name] = []
                    index.symbols[sym.name].append(sym)
            except Exception:
                pass

    return index


def resolve_import(
    module_path: str,
    index: WorkspaceIndex,
    max_suggestions: int = 3,
) -> ImportResolution:
    """Check if a module import resolves. If not, suggest alternatives."""
    if module_path in index.modules:
        filepath = index.modules[module_path]
        symbols = _get_module_symbols(index, module_path)
        return ImportResolution(
            module_path=module_path,
            resolved=True,
            filepath=filepath,
            available_symbols=symbols,
        )

    pkg_init = module_path + ".__init__"
    if pkg_init in index.modules:
        return ImportResolution(
            module_path=module_path,
            resolved=True,
            filepath=index.modules[pkg_init],
        )

    suggestions = _suggest_modules(module_path, index, max_suggestions)
    return ImportResolution(
        module_path=module_path,
        resolved=False,
        suggestions=suggestions,
    )


def search_imports(
    symbol_name: str,
    index: WorkspaceIndex,
) -> str:
    """Search for a symbol across all workspace modules.

    Returns full signatures + docstrings, formatted for AI prompts.
    """
    if symbol_name in index.symbols:
        matches = index.symbols[symbol_name]
        lines = [f"Found '{symbol_name}' in {len(matches)} module(s):\n"]
        for m in matches:
            lines.append(_format_symbol_detail(m))
        return "\n".join(lines)

    # Fuzzy match
    all_names = list(index.symbols.keys())
    close = get_close_matches(symbol_name, all_names, n=5, cutoff=0.6)
    if close:
        lines = [f"No exact match for '{symbol_name}'. Similar symbols:\n"]
        for name in close:
            for m in index.symbols[name][:2]:
                lines.append(_format_symbol_detail(m))
        return "\n".join(lines)

    return f"No matches found for '{symbol_name}' in the workspace."


def list_all_imports(
    module_path: str,
    index: WorkspaceIndex,
) -> str:
    """List every importable symbol in a module with full signatures.

    Usage: list_all_imports("app.core.auth", index)
    """
    if module_path not in index.modules:
        # Try fuzzy
        close = get_close_matches(module_path, index.all_module_paths, n=3, cutoff=0.5)
        if close:
            lines = [f"Module '{module_path}' not found. Did you mean:"]
            for mp in close:
                lines.append(f"  - {mp}")
            return "\n".join(lines)
        return f"Module '{module_path}' not found in workspace."

    # Gather all symbols from this module
    module_symbols = []
    for name, sym_list in index.symbols.items():
        for sym in sym_list:
            if sym.module_path == module_path:
                module_symbols.append(sym)

    if not module_symbols:
        return f"Module '{module_path}' exists at {index.modules[module_path]} but exports no public symbols."

    lines = [
        f"Module: {module_path} ({index.modules[module_path]})",
        f"Importable symbols ({len(module_symbols)}):\n",
    ]
    for sym in sorted(module_symbols, key=lambda s: (s.kind != "class", s.kind != "function", s.name)):
        lines.append(_format_symbol_detail(sym))

    lines.append(f"\nImport with: from {module_path} import {', '.join(s.name for s in module_symbols)}")
    return "\n".join(lines)


def format_resolution_hint(resolution: ImportResolution) -> str:
    """Format a resolution result as a hint for the AI."""
    if resolution.resolved:
        if resolution.available_symbols:
            syms = ", ".join(resolution.available_symbols[:10])
            more = f" (+{len(resolution.available_symbols) - 10} more)" if len(resolution.available_symbols) > 10 else ""
            return f"Module '{resolution.module_path}' exists at {resolution.filepath}. Available: {syms}{more}"
        return f"Module '{resolution.module_path}' exists at {resolution.filepath}."

    if resolution.suggestions:
        lines = [f"Module '{resolution.module_path}' does not exist. Did you mean:"]
        for s in resolution.suggestions:
            lines.append(f"  - {s}")
        return "\n".join(lines)

    return f"Module '{resolution.module_path}' does not exist and no close matches found."


# ── Formatting ───────────────────────────────────────────────────────────────


def _format_symbol_detail(sym: ExportedSymbol) -> str:
    """Format a symbol with full signature and docstring for AI context."""
    lines = []

    if sym.kind == "function":
        sig = sym.signature or "()"
        lines.append(f"  def {sym.name}{sig}")
        lines.append(f"      # from {sym.module_path} import {sym.name}")
        if sym.docstring:
            lines.append(f"      \"\"\"{sym.docstring}\"\"\"")

    elif sym.kind == "class":
        lines.append(f"  class {sym.name}:")
        lines.append(f"      # from {sym.module_path} import {sym.name}")
        if sym.docstring:
            lines.append(f"      \"\"\"{sym.docstring}\"\"\"")
        if sym.attributes:
            lines.append(f"      Attributes: {', '.join(sym.attributes)}")
        if sym.methods:
            for method_sig in sym.methods[:10]:
                lines.append(f"      {method_sig}")
            if len(sym.methods) > 10:
                lines.append(f"      ... +{len(sym.methods) - 10} more methods")

    elif sym.kind == "variable":
        lines.append(f"  {sym.name} = ...")
        lines.append(f"      # from {sym.module_path} import {sym.name}")

    elif sym.kind == "import":
        lines.append(f"  {sym.name}  (re-exported)")
        lines.append(f"      # from {sym.module_path} import {sym.name}")

    lines.append("")  # blank line between symbols
    return "\n".join(lines)


# ── AST extraction ───────────────────────────────────────────────────────────


def _filepath_to_module(rel_path: str) -> Optional[str]:
    if not rel_path.endswith(".py"):
        return None
    parts = PurePosixPath(rel_path).parts
    module_parts = list(parts[:-1]) + [parts[-1][:-3]]
    return ".".join(module_parts)


def _get_docstring(node) -> str:
    """Extract the first paragraph of a docstring from an AST node."""
    ds = ast.get_docstring(node)
    if not ds:
        return ""
    # First paragraph only — keep it concise for AI context
    first_para = ds.split("\n\n")[0].strip()
    # Collapse whitespace
    return " ".join(first_para.split())


def _get_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Extract a function signature string from AST.

    Returns e.g. "(credentials: HTTPAuthorizationCredentials, db: AsyncSession) -> User"
    """
    args = []
    all_args = node.args

    # Positional args
    defaults_offset = len(all_args.args) - len(all_args.defaults)
    for i, arg in enumerate(all_args.args):
        if arg.arg == "self" or arg.arg == "cls":
            continue
        part = arg.arg
        if arg.annotation:
            part += f": {_annotation_str(arg.annotation)}"
        # Default value
        default_idx = i - defaults_offset
        if default_idx >= 0 and default_idx < len(all_args.defaults):
            part += " = ..."
        args.append(part)

    # *args
    if all_args.vararg:
        part = f"*{all_args.vararg.arg}"
        if all_args.vararg.annotation:
            part += f": {_annotation_str(all_args.vararg.annotation)}"
        args.append(part)

    # Keyword-only args
    kw_defaults_map = {i: d for i, d in enumerate(all_args.kw_defaults) if d is not None}
    for i, arg in enumerate(all_args.kwonlyargs):
        part = arg.arg
        if arg.annotation:
            part += f": {_annotation_str(arg.annotation)}"
        if i in kw_defaults_map:
            part += " = ..."
        args.append(part)

    # **kwargs
    if all_args.kwarg:
        part = f"**{all_args.kwarg.arg}"
        if all_args.kwarg.annotation:
            part += f": {_annotation_str(all_args.kwarg.annotation)}"
        args.append(part)

    sig = f"({', '.join(args)})"

    # Return annotation
    if node.returns:
        sig += f" -> {_annotation_str(node.returns)}"

    return sig


def _annotation_str(node) -> str:
    """Convert an annotation AST node to a readable string."""
    try:
        return ast.unparse(node)
    except Exception:
        return "..."


def _extract_class_details(node: ast.ClassDef) -> tuple[list[str], list[str]]:
    """Extract method signatures and attribute names from a class."""
    methods = []
    attributes = set()

    for item in ast.iter_child_nodes(node):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not item.name.startswith("_") or item.name == "__init__":
                sig = _get_function_signature(item)
                prefix = "async " if isinstance(item, ast.AsyncFunctionDef) else ""
                doc = _get_docstring(item)
                method_str = f"def {item.name}{sig}"
                if doc:
                    method_str += f"  # {doc[:60]}"
                methods.append(prefix + method_str)

                # Extract self.X assignments from __init__
                if item.name == "__init__":
                    for stmt in ast.walk(item):
                        if isinstance(stmt, ast.Assign):
                            for target in stmt.targets:
                                if (isinstance(target, ast.Attribute)
                                    and isinstance(target.value, ast.Name)
                                    and target.value.id == "self"
                                    and not target.attr.startswith("_")):
                                    attributes.add(target.attr)

        elif isinstance(item, ast.AnnAssign):
            if isinstance(item.target, ast.Name) and not item.target.id.startswith("_"):
                attr = item.target.id
                if item.annotation:
                    attr += f": {_annotation_str(item.annotation)}"
                attributes.add(attr)

    return methods, sorted(attributes)


def _extract_exports(content: str, module_path: str, filepath: str) -> List[ExportedSymbol]:
    """Extract top-level exported names with signatures and docstrings."""
    exports = []
    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        return exports

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                sig = _get_function_signature(node)
                doc = _get_docstring(node)
                exports.append(ExportedSymbol(
                    name=node.name,
                    module_path=module_path,
                    filepath=filepath,
                    kind="function",
                    signature=sig,
                    docstring=doc,
                ))

        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                doc = _get_docstring(node)
                methods, attributes = _extract_class_details(node)
                exports.append(ExportedSymbol(
                    name=node.name,
                    module_path=module_path,
                    filepath=filepath,
                    kind="class",
                    docstring=doc,
                    methods=methods,
                    attributes=attributes,
                ))

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    exports.append(ExportedSymbol(
                        name=target.id,
                        module_path=module_path,
                        filepath=filepath,
                        kind="variable",
                    ))

        elif isinstance(node, ast.ImportFrom):
            # Only track re-exports from workspace modules (app.*, not
            # fastapi, sqlalchemy, etc.) — third-party re-exports are
            # noise that clutters the import search results.
            if node.module and node.names and node.module.startswith("app"):
                for alias in node.names:
                    name = alias.asname or alias.name
                    if not name.startswith("_") and name != "*":
                        exports.append(ExportedSymbol(
                            name=name,
                            module_path=module_path,
                            filepath=filepath,
                            kind="import",
                        ))

    return exports


# ── Internal helpers ─────────────────────────────────────────────────────────


def _get_module_symbols(index: WorkspaceIndex, module_path: str) -> List[str]:
    result = []
    for name, syms in index.symbols.items():
        for s in syms:
            if s.module_path == module_path:
                result.append(name)
                break
    return sorted(set(result))


def _suggest_modules(
    target: str,
    index: WorkspaceIndex,
    max_suggestions: int = 3,
) -> List[str]:
    suggestions = []

    leaf = target.rsplit(".", 1)[-1] if "." in target else target
    for mp in index.all_module_paths:
        mp_leaf = mp.rsplit(".", 1)[-1] if "." in mp else mp
        if mp_leaf == leaf and mp != target:
            syms = _get_module_symbols(index, mp)
            hint = f"from {mp} import ..." if not syms else f"from {mp} import {', '.join(syms[:5])}"
            suggestions.append(hint)

    close = get_close_matches(target, index.all_module_paths, n=max_suggestions * 2, cutoff=0.5)
    for mp in close:
        syms = _get_module_symbols(index, mp)
        hint = f"from {mp} import ..." if not syms else f"from {mp} import {', '.join(syms[:5])}"
        if hint not in suggestions:
            suggestions.append(hint)

    return suggestions[:max_suggestions]
