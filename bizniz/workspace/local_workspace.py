# bizniz/workspace/local_workspace.py

from pathlib import Path
from typing import Optional, Union

from bizniz.workspace.base_workspace import BaseWorkspace


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
        bizniz_db=None,
        project_id: Optional[str] = None,
        service_name: Optional[str] = None,
    ):
        root_path = Path(root).expanduser().resolve()

        if root_path.exists():
            if not root_path.is_dir():
                raise ValueError(f"Workspace path is not a directory: {root_path}")
        else:
            if create:
                root_path.mkdir(parents=True, exist_ok=True)
            else:
                raise FileNotFoundError(f"Workspace directory does not exist: {root_path}")

        super().__init__(
            root_path,
            bizniz_db=bizniz_db,
            project_id=project_id,
            service_name=service_name,
        )

    @classmethod
    def from_name(cls, name: str, parent: str | Path = "~", **kwargs) -> "LocalWorkspace":
        """Create a workspace from a human-readable name.

        Args:
            name: Human-readable name like "Fraydit Solutions"
            parent: Parent directory (default: home directory)

        Returns:
            LocalWorkspace at {parent}/{slugified_name}
        """
        from bizniz.workspace.naming import slugify
        slug = slugify(name)
        root = Path(parent).expanduser() / slug
        return cls(root=root, **kwargs)

    def __repr__(self) -> str:
        return f"<LocalWorkspace root={self.root}>"