"""File I/O tool factories for v2 tool-loop agents.

Pattern: each ``make_*`` returns a ``ToolHandler`` (callable taking
the parsed action dict, returning a string result for the conversation).
The factory closes over the workspace + any other instance state, so
agents can compose their tool dict at construction time without
exposing globals.

Example::

    handlers = {
        "view_file": make_view_file(self._workspace),
        "write_file": make_write_file(self._workspace),
        "list_directory": make_list_directory(self._workspace),
        "search_files": make_search_files(self._workspace),
    }
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict

from bizniz.workspace.base_workspace import BaseWorkspace


ToolHandler = Callable[[Dict], str]


_MAX_RESULT_BYTES = 64 * 1024  # 64 KB cap per tool result
_MAX_FILE_BYTES = 200 * 1024   # 200 KB cap on file reads (above this = "too large")


def _truncate(s: str, n: int = _MAX_RESULT_BYTES) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n\n... (truncated, total {len(s)} bytes)"


def make_view_file(workspace: BaseWorkspace) -> ToolHandler:
    """Read a file's full content. Action requires ``path``.

    Returns the file content with line numbers, capped at 200 KB. For
    structure-only reads on large files, prefer ``get_file_outline``.
    """
    def handler(action: Dict) -> str:
        path = (action.get("path") or "").strip()
        if not path:
            return "ERROR: view_file requires a 'path'."
        try:
            full = workspace.path(path)
        except Exception as e:
            return f"ERROR: cannot resolve path '{path}': {e}"
        if not full.is_file():
            return f"ERROR: '{path}' is not a file (or doesn't exist)."
        try:
            size = full.stat().st_size
            if size > _MAX_FILE_BYTES:
                return (
                    f"ERROR: '{path}' is {size} bytes (>{_MAX_FILE_BYTES}). "
                    f"Prefer get_file_outline for structure-only reads on "
                    f"large files."
                )
            content = full.read_text(errors="replace")
        except Exception as e:
            return f"ERROR: read failed: {e}"

        # Number lines for easy reference.
        lines = content.splitlines()
        numbered = "\n".join(f"{i + 1:5d}  {line}" for i, line in enumerate(lines))
        return _truncate(f"=== {path} ({len(lines)} lines) ===\n{numbered}")
    return handler


def make_write_file(workspace: BaseWorkspace) -> ToolHandler:
    """Write a file. Action requires ``path`` and ``new_content``.

    Creates parent directories. Overwrites if the file exists. Returns
    a one-line confirmation including the new size.
    """
    def handler(action: Dict) -> str:
        path = (action.get("path") or "").strip()
        new_content = action.get("new_content")
        if not path:
            return "ERROR: write_file requires a 'path'."
        if new_content is None:
            return "ERROR: write_file requires 'new_content' (use empty string for an empty file)."
        try:
            full = workspace.path(path)
        except Exception as e:
            return f"ERROR: cannot resolve path '{path}': {e}"
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(new_content)
        except Exception as e:
            return f"ERROR: write failed: {e}"
        return (
            f"wrote '{path}' ({len(new_content)} bytes, "
            f"{len(new_content.splitlines())} lines)"
        )
    return handler


def make_list_directory(workspace: BaseWorkspace) -> ToolHandler:
    """List entries in a directory. Action takes ``path`` (default '.').

    Output: one entry per line, trailing '/' for directories. Excludes
    common noise (__pycache__, node_modules, .pytest_cache, .venv).
    For deeper, structured exploration use ``get_workspace_tree``.
    """
    EXCLUDE = {"__pycache__", "node_modules", ".pytest_cache", ".venv",
               ".git", ".angular", ".next", "dist", "build", ".cache"}

    def handler(action: Dict) -> str:
        path = (action.get("path") or ".").strip()
        try:
            full = workspace.path(path)
        except Exception as e:
            return f"ERROR: cannot resolve path '{path}': {e}"
        if not full.is_dir():
            return f"ERROR: '{path}' is not a directory."
        try:
            entries = sorted(full.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except Exception as e:
            return f"ERROR: list failed: {e}"
        lines = []
        for e in entries:
            if e.name in EXCLUDE:
                continue
            suffix = "/" if e.is_dir() else ""
            lines.append(f"{e.name}{suffix}")
        if not lines:
            return f"(empty: {path})"
        return f"=== {path} ({len(lines)} entries) ===\n" + "\n".join(lines)
    return handler


def make_search_files(workspace: BaseWorkspace) -> ToolHandler:
    """Regex grep over the workspace. Action takes ``pattern`` (the
    regex, in the ``path`` field for compatibility with the universal
    schema).

    Returns matching lines with ``<file>:<line>: <text>``. Excludes
    test caches / build artifacts.
    """
    import re

    EXCLUDE_DIRS = {"__pycache__", "node_modules", ".pytest_cache", ".venv",
                    ".git", ".angular", ".next", "dist", "build", ".cache"}
    BINARY_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                       ".pdf", ".so", ".dll", ".dylib", ".woff", ".woff2",
                       ".ttf", ".eot", ".ico", ".db", ".sqlite"}
    MAX_HITS = 200

    def handler(action: Dict) -> str:
        pattern = (action.get("path") or action.get("pattern") or "").strip()
        if not pattern:
            return "ERROR: search_files requires a regex 'path' (or 'pattern')."
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"ERROR: invalid regex {pattern!r}: {e}"

        root = Path(workspace.root) if hasattr(workspace, "root") else None
        if root is None or not root.is_dir():
            return "ERROR: workspace root unavailable."

        hits = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(seg in EXCLUDE_DIRS for seg in path.parts):
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            try:
                if path.stat().st_size > 500_000:
                    continue
                text = path.read_text(errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = path.relative_to(root)
                    hits.append(f"{rel}:{i}: {line.strip()}")
                    if len(hits) >= MAX_HITS:
                        break
            if len(hits) >= MAX_HITS:
                break

        if not hits:
            return f"(no matches for {pattern!r})"
        suffix = f"\n... ({MAX_HITS}+ matches; refine pattern)" if len(hits) >= MAX_HITS else ""
        return f"{len(hits)} match(es) for {pattern!r}:\n" + "\n".join(hits) + suffix
    return handler


def build_file_io_handlers(workspace: BaseWorkspace) -> Dict[str, ToolHandler]:
    """Convenience: build the standard file-I/O tool dict."""
    return {
        "view_file": make_view_file(workspace),
        "write_file": make_write_file(workspace),
        "list_directory": make_list_directory(workspace),
        "search_files": make_search_files(workspace),
    }
