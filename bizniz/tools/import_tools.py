"""Import resolution tools for AI agents.

Provides two capabilities:

1. **resolve_import(module_path)** — check if an import resolves in
   the workspace, and if not, suggest the closest matches.

2. **search_imports(symbol_name)** — find all workspace files that
   export a given symbol name (function, class, variable).

These tools are language-specific. Python support is built-in;
TypeScript is a future extension.

Usage by agents:
    from bizniz.tools.import_tools import (
        resolve_import,
        search_imports,
        build_workspace_index,
    )
    index = build_workspace_index(workspace)
    result = resolve_import("app.api.deps", index)
    # → "Module 'app.api.deps' not found. Did you mean:
    #      from app.core.auth import get_current_user, require_roles
    #      from app.api.routes.auth import router"
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set


@dataclass
class ExportedSymbol:
    """A symbol exported by a workspace module."""
    name: str           # e.g. "get_current_user"
    module_path: str    # e.g. "app.core.auth"
    filepath: str       # e.g. "app/core/auth.py"
    kind: str           # "function", "class", "variable", "import"


@dataclass
class WorkspaceIndex:
    """Index of all importable modules and symbols in a workspace."""
    # module_path → filepath  (e.g. "app.core.auth" → "app/core/auth.py")
    modules: Dict[str, str] = field(default_factory=dict)
    # symbol_name → list of ExportedSymbol  (e.g. "get_current_user" → [...])
    symbols: Dict[str, List[ExportedSymbol]] = field(default_factory=dict)
    # All module paths for fuzzy matching
    all_module_paths: List[str] = field(default_factory=list)


@dataclass
class ImportResolution:
    """Result of trying to resolve an import."""
    module_path: str
    resolved: bool
    filepath: Optional[str] = None
    # If not resolved, suggestions for what the user might have meant
    suggestions: List[str] = field(default_factory=list)
    # Specific symbols available if the module IS resolved
    available_symbols: List[str] = field(default_factory=list)


def build_workspace_index(workspace_root: Path) -> WorkspaceIndex:
    """Walk the workspace and index all Python modules + exported symbols."""
    index = WorkspaceIndex()

    for dirpath, dirnames, filenames in os.walk(workspace_root):
        # Skip noise directories
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

            # Convert filepath to module path
            module_path = _filepath_to_module(rel_path)
            if module_path is None:
                continue

            index.modules[module_path] = rel_path
            index.all_module_paths.append(module_path)

            # Parse and extract exported symbols
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

    # Direct match
    if module_path in index.modules:
        filepath = index.modules[module_path]
        symbols = _get_module_symbols(index, module_path)
        return ImportResolution(
            module_path=module_path,
            resolved=True,
            filepath=filepath,
            available_symbols=symbols,
        )

    # Check if it's a package (has __init__.py)
    pkg_init = module_path + ".__init__"
    if pkg_init in index.modules:
        return ImportResolution(
            module_path=module_path,
            resolved=True,
            filepath=index.modules[pkg_init],
        )

    # Not found — fuzzy match against all module paths
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

    Returns a formatted string suitable for injection into AI prompts.
    """
    if symbol_name in index.symbols:
        matches = index.symbols[symbol_name]
        lines = [f"Found '{symbol_name}' in {len(matches)} module(s):"]
        for m in matches:
            lines.append(f"  from {m.module_path} import {m.name}  # {m.kind} in {m.filepath}")
        return "\n".join(lines)

    # Fuzzy match on symbol names
    all_names = list(index.symbols.keys())
    close = get_close_matches(symbol_name, all_names, n=5, cutoff=0.6)
    if close:
        lines = [f"No exact match for '{symbol_name}'. Similar symbols:"]
        for name in close:
            for m in index.symbols[name][:2]:  # cap per symbol
                lines.append(f"  from {m.module_path} import {m.name}  # {m.kind}")
        return "\n".join(lines)

    return f"No matches found for '{symbol_name}' in the workspace."


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


# ── Private helpers ──────────────────────────────────────────────────────────


def _filepath_to_module(rel_path: str) -> Optional[str]:
    """Convert a relative filepath to a Python module path.

    "app/core/auth.py" → "app.core.auth"
    "app/core/__init__.py" → "app.core.__init__"
    """
    if not rel_path.endswith(".py"):
        return None

    parts = PurePosixPath(rel_path).parts
    # Strip .py from the last part
    module_parts = list(parts[:-1]) + [parts[-1][:-3]]
    return ".".join(module_parts)


def _extract_exports(content: str, module_path: str, filepath: str) -> List[ExportedSymbol]:
    """Extract top-level exported names from a Python module."""
    exports = []
    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        return exports

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            if not node.name.startswith("_"):
                exports.append(ExportedSymbol(
                    name=node.name, module_path=module_path,
                    filepath=filepath, kind="function",
                ))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                exports.append(ExportedSymbol(
                    name=node.name, module_path=module_path,
                    filepath=filepath, kind="class",
                ))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    exports.append(ExportedSymbol(
                        name=target.id, module_path=module_path,
                        filepath=filepath, kind="variable",
                    ))
        elif isinstance(node, ast.ImportFrom):
            # Re-exports: "from foo import bar" at top level
            if node.names:
                for alias in node.names:
                    name = alias.asname or alias.name
                    if not name.startswith("_") and name != "*":
                        exports.append(ExportedSymbol(
                            name=name, module_path=module_path,
                            filepath=filepath, kind="import",
                        ))

    return exports


def _get_module_symbols(index: WorkspaceIndex, module_path: str) -> List[str]:
    """Get all symbol names exported by a specific module."""
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
    """Suggest closest-matching module paths for a failed import.

    Uses both difflib fuzzy matching and structural matching
    (same leaf name in a different package).
    """
    suggestions = []

    # 1. Exact leaf match — e.g. "app.api.deps" → find anything ending in ".deps"
    leaf = target.rsplit(".", 1)[-1] if "." in target else target
    for mp in index.all_module_paths:
        mp_leaf = mp.rsplit(".", 1)[-1] if "." in mp else mp
        if mp_leaf == leaf and mp != target:
            # Show what symbols this module exports
            syms = _get_module_symbols(index, mp)
            hint = f"from {mp} import ..." if not syms else f"from {mp} import {', '.join(syms[:5])}"
            suggestions.append(hint)

    # 2. Fuzzy match on full module path
    close = get_close_matches(target, index.all_module_paths, n=max_suggestions * 2, cutoff=0.5)
    for mp in close:
        syms = _get_module_symbols(index, mp)
        hint = f"from {mp} import ..." if not syms else f"from {mp} import {', '.join(syms[:5])}"
        if hint not in suggestions:
            suggestions.append(hint)

    return suggestions[:max_suggestions]
