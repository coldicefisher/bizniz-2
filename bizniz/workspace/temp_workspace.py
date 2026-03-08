# bizniz/workspace/temp_workspace.py

from pathlib import Path
import tempfile
import shutil
from typing import Optional

from bizniz.workspace.base_workspace import BaseWorkspace


class TempWorkspace(BaseWorkspace):
    """
    TempWorkspace creates a temporary workspace directory.

    It is intended for short-lived execution runs (Autocoder,
    AutoTester, AutoEngineer, etc.).

    The directory is automatically removed when cleanup() is called
    or when used as a context manager.
    """

    def __init__(
        self,
        *,
        prefix: str = "bizniz_",
        root: Optional[str | Path] = None
    ):
        if root is None:
            self._temp_dir = tempfile.mkdtemp(prefix=prefix)
            root_path = Path(self._temp_dir)
            self._owns_dir = True
        else:
            root_path = Path(root).resolve()
            root_path.mkdir(parents=True, exist_ok=True)
            self._temp_dir = None
            self._owns_dir = False

        super().__init__(root_path)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        """Delete the temporary workspace directory."""
        if self._owns_dir and self.root.exists():
            shutil.rmtree(self.root)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()

    # ------------------------------------------------------------------

    def __repr__(self):
        return f"<TempWorkspace root={self.root}>"