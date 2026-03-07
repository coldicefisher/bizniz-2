# verix/workspace/local_workspace.py

from pathlib import Path
from typing import Union

from verix.workspace.base_workspace import BaseWorkspace


class LocalWorkspace(BaseWorkspace):
    """
    LocalWorkspace represents a persistent workspace located on the
    host filesystem.

    Unlike TempWorkspace, this directory is not automatically deleted
    and is intended for long-lived projects or repositories.
    """

    def __init__(
        self,
        root: Union[str, Path],
        *,
        create: bool = True,
    ):
        root_path = Path(root).resolve()

        if root_path.exists():
            if not root_path.is_dir():
                raise ValueError(f"Workspace path is not a directory: {root_path}")
        else:
            if create:
                root_path.mkdir(parents=True, exist_ok=True)
            else:
                raise FileNotFoundError(f"Workspace directory does not exist: {root_path}")

        super().__init__(root_path)

    def __repr__(self) -> str:
        return f"<LocalWorkspace root={self.root}>"