"""
Project

Represents a bizniz project rooted at a directory with this structure:

    project_root/
    ├── .bizniz/
    │   └── project.db          (project-level SQLite)
    ├── dockerfiles/
    │   └── development/
    │       ├── docker-compose.yml
    │       ├── .env
    │       ├── backend/         (service workspace dir)
    │       ├── frontend/        (service workspace dir)
    │       └── ...
"""

from pathlib import Path
from typing import Optional, List, Dict

from bizniz.workspace.local_workspace import LocalWorkspace


class Project:

    def __init__(self, root: str | Path, project_name: str):
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._project_name = project_name
        self._db = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def project_name(self) -> str:
        return self._project_name

    @property
    def dev_root(self) -> Path:
        """Returns root / dockerfiles / development"""
        return self._root / "dockerfiles" / "development"

    @property
    def db(self) -> "ProjectDB":
        """Lazily create and return the project-level SQLite database."""
        if self._db is None:
            from bizniz.project.project_db import ProjectDB
            self._db = ProjectDB(self)
        return self._db

    def create_structure(self):
        """Create the full project directory structure."""
        self.dev_root.mkdir(parents=True, exist_ok=True)

    def get_service_workspace(self, service_name: str) -> LocalWorkspace:
        """Returns a LocalWorkspace at dev_root / service_name"""
        ws_path = self.dev_root / service_name
        return LocalWorkspace(root=str(ws_path))

    def write_docker_compose(self, content: str):
        """Write docker-compose.yml to dev_root"""
        self.dev_root.mkdir(parents=True, exist_ok=True)
        compose_path = self.dev_root / "docker-compose.yml"
        compose_path.write_text(content)

    def write_env_file(self, content: str):
        """Write .env to dev_root"""
        self.dev_root.mkdir(parents=True, exist_ok=True)
        env_path = self.dev_root / ".env"
        env_path.write_text(content)

    def get_issue_history(self) -> List[dict]:
        """Get all issues across all services."""
        return self.db.get_all_issues()

    def get_architecture_changes(self) -> List[dict]:
        """Get architecture change history."""
        return self.db.get_architecture_changes()

    def get_service_status(self) -> List[dict]:
        """Get current status of all services."""
        return self.db.get_services()

    @classmethod
    def from_name(cls, project_name: str, parent: str | Path = "~") -> "Project":
        """Create a project from a human-readable name."""
        from bizniz.workspace.naming import slugify
        slug = slugify(project_name)
        root = Path(parent).expanduser() / slug
        return cls(root=root, project_name=project_name)

    def __repr__(self) -> str:
        return f"<Project name={self._project_name!r} root={self._root}>"
