"""
Project

Represents a bizniz project rooted at a directory with this structure:

    project_root/
    ├── .bizniz/
    │   └── project.db
    ├── backend/                 (service source code)
    │   ├── src/
    │   └── tests/
    ├── frontend/                (service source code)
    │   ├── src/
    │   └── tests/
    └── dockerfiles/
        └── development/
            ├── docker-compose.yml
            ├── .env
            ├── backend/         (Dockerfile, entrypoint, requirements)
            └── frontend/        (Dockerfile, package.json)
"""

from pathlib import Path
from typing import Optional, List, Dict

from bizniz.workspace.local_workspace import LocalWorkspace


class Project:

    def __init__(self, root: str | Path, project_name: str, bizniz_db=None):
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._project_name = project_name
        self._bizniz_db = bizniz_db
        self._db = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def project_name(self) -> str:
        return self._project_name

    @property
    def dev_root(self) -> Path:
        """Returns root / dockerfiles / development (Docker configs live here)."""
        return self._root / "dockerfiles" / "development"

    @property
    def docker_root(self) -> Path:
        """Alias for dev_root — Docker build configs for each service."""
        return self.dev_root

    @property
    def db(self):
        """Lazily create and return the project-level database.

        When a unified BiznizDB is provided, returns a ProjectScope
        backed by the shared database.  Otherwise falls back to a
        per-project SQLite file.
        """
        if self._db is None:
            if self._bizniz_db is not None:
                self._db = self._bizniz_db.for_project(self._project_name)
            else:
                from bizniz.project.project_db import ProjectDB
                self._db = ProjectDB(self)
        return self._db

    def create_structure(self):
        """Create the full project directory structure."""
        self.dev_root.mkdir(parents=True, exist_ok=True)

    def get_docker_service_dir(self, service_name: str) -> Path:
        """Returns the Docker config directory for a service (Dockerfile, requirements, etc.)."""
        docker_dir = self.docker_root / service_name
        docker_dir.mkdir(parents=True, exist_ok=True)
        return docker_dir

    def get_service_workspace(self, service_name: str) -> LocalWorkspace:
        """Returns a LocalWorkspace at project_root / service_name (source code)."""
        ws_path = self._root / service_name
        return LocalWorkspace(
            root=str(ws_path),
            bizniz_db=self._bizniz_db,
            project_id=self._project_name,
            service_name=service_name,
        )

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
    def from_name(cls, project_name: str, parent: str | Path = "~", bizniz_db=None) -> "Project":
        """Create a project from a human-readable name."""
        from bizniz.workspace.naming import slugify
        slug = slugify(project_name)
        root = Path(parent).expanduser() / slug
        return cls(root=root, project_name=project_name, bizniz_db=bizniz_db)

    def __repr__(self) -> str:
        return f"<Project name={self._project_name!r} root={self._root}>"
