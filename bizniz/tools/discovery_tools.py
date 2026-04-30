"""
Shared discovery tools for workspace exploration.

These tools are used by all agentic agents (coder, tester, agentic debugger)
to discover file contents, search code, and list directories on demand rather than
receiving everything inline in the prompt.
"""

import subprocess
from pathlib import Path
from typing import Set

from bizniz.workspace.base_workspace import BaseWorkspace


TREE_EXCLUDE_DIRS: Set[str] = {
    "node_modules", "__pycache__", ".git", ".bizniz",
    "dist", "build", ".next",
}
TREE_MAX_FILES: int = 50


def tool_view_file(workspace: BaseWorkspace, path: str) -> str:
    """Read a file from the workspace."""
    try:
        if not path:
            return "ERROR: No path provided."
        content = workspace.read_file(path=path)
        if content is None:
            return f"ERROR: File '{path}' not found or empty."
        lines = content.split("\n")
        if len(lines) > 500:
            return "\n".join(lines[:500]) + f"\n\n... (truncated, {len(lines)} total lines)"
        return content
    except Exception as e:
        return f"ERROR: Could not read '{path}': {e}"


def tool_list_directory(workspace: BaseWorkspace, path: str) -> str:
    """List files in a directory or the full workspace tree."""
    try:
        if not path or path == ".":
            tree = workspace.tree()
            if tree:
                if isinstance(tree, list):
                    return "\n".join(str(f) for f in sorted(tree))
                return str(tree)
            files = workspace.list_relative_files()
            return "\n".join(str(f) for f in sorted(files))

        all_files = workspace.list_relative_files()
        prefix = path.rstrip("/") + "/"
        matching = [str(f) for f in all_files if str(f).startswith(prefix) or str(f) == path]
        if matching:
            return "\n".join(sorted(matching))
        return f"No files found under '{path}'."
    except Exception as e:
        return f"ERROR: Could not list directory '{path}': {e}"


def tool_search_files(workspace: BaseWorkspace, pattern: str) -> str:
    """Search for a regex pattern across all workspace files."""
    try:
        if not pattern:
            return "ERROR: No search pattern provided."
        workspace_root = str(workspace.root)
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", "-E", pattern, "."],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip()
        if not output:
            return f"No matches found for pattern '{pattern}'."
        lines = output.split("\n")
        if len(lines) > 100:
            return "\n".join(lines[:100]) + f"\n\n... ({len(lines)} total matches, showing first 100)"
        return output
    except subprocess.TimeoutExpired:
        return "ERROR: Search timed out after 30 seconds."
    except Exception as e:
        return f"ERROR: Search failed: {e}"


def build_filtered_file_tree(workspace: BaseWorkspace) -> str:
    """Build a filtered file tree string, excluding noisy directories."""
    try:
        all_files = workspace.list_relative_files()
        filtered = []
        for f in sorted(str(fp) for fp in all_files):
            segments = Path(f).parts
            if any(seg in TREE_EXCLUDE_DIRS for seg in segments):
                continue
            filtered.append(f)

        if not filtered:
            return "(empty workspace)"

        if len(filtered) > TREE_MAX_FILES:
            tree = "\n".join(filtered[:TREE_MAX_FILES])
            tree += f"\n... ({len(filtered) - TREE_MAX_FILES} more files, use list_directory to explore)"
            return tree
        return "\n".join(filtered)
    except Exception:
        return "(could not list files)"
