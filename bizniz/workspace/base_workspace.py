# bizniz/workspace/base_workspace.py

import os
import subprocess
from pathlib import Path
from typing import List, Optional


class BaseWorkspace:
    """
    BaseWorkspace represents a filesystem workspace containing
    a project (source code, tests, configs, etc).

    It provides file operations and optional git operations.
    Execution environments operate *against* the workspace but
    the workspace itself never executes code.
    """

    def __init__(self, root: str | Path):
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """Root directory of the workspace."""
        return self._root

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def path(self, relative_path: str | Path) -> Path:
        """Resolve a path relative to the workspace root."""
        return self._root / relative_path

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def write_file(self, path: str | Path, content: str) -> Path:
        """
        Write a file inside the workspace, creating directories if needed.
        """
        full_path = self.path(path)
        full_path.parent.mkdir(parents=True, exist_ok=True)

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        return full_path

    def read_file(self, path: str | Path) -> str:
        """Read a file from the workspace."""
        full_path = self.path(path)

        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()

    def delete_file(self, path: str | Path):
        """Delete a file."""
        full_path = self.path(path)

        if full_path.exists():
            full_path.unlink()

    # ------------------------------------------------------------------
    # Directory operations
    # ------------------------------------------------------------------

    def make_dir(self, path: str | Path):
        """Create a directory inside the workspace."""
        self.path(path).mkdir(parents=True, exist_ok=True)

    def list_files(self) -> List[Path]:
        """Return all files in the workspace."""
        return [p for p in self._root.rglob("*") if p.is_file()]

    def list_relative_files(self) -> List[str]:
        """Return files relative to workspace root."""
        return [
            str(p.relative_to(self._root))
            for p in self._root.rglob("*")
            if p.is_file()
        ]

    # ------------------------------------------------------------------
    # Git operations
    # ------------------------------------------------------------------

    def init_git(self):
        """Initialize a git repository."""
        subprocess.run(
            ["git", "init"],
            cwd=self._root,
            check=True,
            capture_output=True,
        )

    def git_add_all(self):
        """Stage all changes."""
        subprocess.run(
            ["git", "add", "."],
            cwd=self._root,
            check=True,
            capture_output=True,
        )

    def git_commit(self, message: str):
        """Commit staged changes."""
        self.git_add_all()

        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=self._root,
            check=True,
            capture_output=True,
        )

    def git_diff(self) -> str:
        """Return git diff."""
        result = subprocess.run(
            ["git", "diff"],
            cwd=self._root,
            capture_output=True,
            text=True,
            check=False,
        )

        return result.stdout

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def exists(self, path: str | Path) -> bool:
        """Check if a file exists in workspace."""
        return self.path(path).exists()

    def absolute_path(self, path: str | Path) -> Path:
        """Return absolute path of a file inside workspace."""
        return self.path(path)

    # ------------------------------------------------------------------
    # Debug helpers
    # ------------------------------------------------------------------

    def tree(self) -> List[str]:
        """Return a tree of workspace files."""
        return self.list_relative_files()

    # ------------------------------------------------------------------

    def __repr__(self):
        return f"<Workspace root={self._root}>"