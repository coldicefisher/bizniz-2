# bizniz/workspace/base_workspace.py

import os
import subprocess
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from bizniz.workspace.workspace_db import WorkspaceDB
    from bizniz.db.bizniz_db import BiznizDB


class BaseWorkspace:
    """
    BaseWorkspace represents a filesystem workspace containing
    a project (source code, tests, configs, etc).

    It provides file operations and optional git operations.
    Execution environments operate *against* the workspace but
    the workspace itself never executes code.

    The workspace database is lazily created on first access via
    the ``db`` property.  When ``bizniz_db`` is provided, returns
    a WorkspaceScope backed by the unified MySQL/SQLite database.
    Otherwise falls back to a per-workspace SQLite file.
    """

    def __init__(
        self,
        root: str | Path,
        bizniz_db: Optional["BiznizDB"] = None,
        project_id: Optional[str] = None,
        service_name: Optional[str] = None,
    ):
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._db = None
        self._bizniz_db = bizniz_db
        self._project_id = project_id
        self._service_name = service_name

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """Root directory of the workspace."""
        return self._root

    @property
    def db(self):
        """
        Lazily create and return the workspace database.

        When a unified BiznizDB is provided, returns a WorkspaceScope
        backed by the shared database.  Otherwise falls back to a
        per-workspace SQLite file at ``{root}/.bizniz/bizniz.db``.
        """
        if self._db is None:
            if self._bizniz_db is not None:
                self._db = self._bizniz_db.for_workspace(
                    self._project_id, self._service_name,
                )
            else:
                from bizniz.workspace.workspace_db import WorkspaceDB
                self._db = WorkspaceDB(self)
        return self._db

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

    # Directories excluded from file listing — these are never source
    # code and can contain tens of thousands of files (node_modules
    # alone caused 527-file prompts in V11's frontend repair context).
    _EXCLUDE_DIRS = frozenset({
        "node_modules", "__pycache__", ".pytest_cache", ".git",
        ".bizniz", ".egg-info", "dist", "build", ".next", ".nuxt",
        ".venv", "venv",
    })

    def _walk_files(self):
        """Walk workspace files, skipping excluded directories."""
        for dirpath, dirnames, filenames in os.walk(self._root):
            # Prune excluded dirs in-place so os.walk doesn't descend
            dirnames[:] = [
                d for d in dirnames
                if d not in self._EXCLUDE_DIRS
            ]
            for fname in filenames:
                yield Path(dirpath) / fname

    def list_files(self) -> List[Path]:
        """Return all files in the workspace (excludes node_modules, __pycache__, etc.)."""
        return list(self._walk_files())

    def list_relative_files(self) -> List[str]:
        """Return files relative to workspace root (excludes node_modules, __pycache__, etc.)."""
        return [
            str(p.relative_to(self._root))
            for p in self._walk_files()
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
    # Package initialization
    # ------------------------------------------------------------------

    def init_as_package(self, package_name: str, description: str = ""):
        """
        Initialize the workspace as a pip-installable Python package.
        Creates pyproject.toml, package directory, and __init__.py files.
        """
        # Create package directory
        pkg_dir = self._root / package_name
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # Create __init__.py
        init_path = pkg_dir / "__init__.py"
        if not init_path.exists():
            init_path.write_text("")

        # Create tests directory
        tests_dir = self._root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        tests_init = tests_dir / "__init__.py"
        if not tests_init.exists():
            tests_init.write_text("")

        # Create pyproject.toml
        toml_path = self._root / "pyproject.toml"
        if not toml_path.exists():
            toml_content = (
                '[build-system]\n'
                'requires = ["setuptools >= 77.0.3"]\n'
                'build-backend = "setuptools.build_meta"\n'
                '\n'
                '[project]\n'
                f'name = "{package_name}"\n'
                'version = "0.1.0"\n'
                f'description = "{description}"\n'
                'requires-python = ">=3.10"\n'
                '\n'
                '[tool.setuptools]\n'
                f'packages = ["{package_name}"]\n'
                '\n'
                '[tool.pytest.ini_options]\n'
                'testpaths = ["tests"]\n'
            )
            toml_path.write_text(toml_content)

    def create_namespace(self, namespace_path: str):
        """
        Create a namespace directory with __init__.py files for each level.
        E.g. "expense_tracker/models" creates:
          - expense_tracker/__init__.py
          - expense_tracker/models/__init__.py
        """
        parts = Path(namespace_path).parts
        current = self._root
        for part in parts:
            current = current / part
            current.mkdir(parents=True, exist_ok=True)
            init_file = current / "__init__.py"
            if not init_file.exists():
                init_file.write_text("")

    # ------------------------------------------------------------------
    # Debug helpers
    # ------------------------------------------------------------------

    def tree(self) -> List[str]:
        """Return a tree of workspace files."""
        return self.list_relative_files()

    # ------------------------------------------------------------------

    def __repr__(self):
        return f"<Workspace root={self._root}>"