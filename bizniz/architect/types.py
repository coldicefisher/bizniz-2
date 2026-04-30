"""
Auto Architect types.
"""

from typing import List, Optional
from pydantic import BaseModel


class ServiceDefinition(BaseModel):
    """A single service/container in the system architecture."""
    name: str
    service_type: str  # "backend", "frontend", "database", "cache", "proxy", "auth", etc.
    framework: str  # "fastapi", "react", "angular", "nginx", "redis", "postgres", etc.
    language: str  # "python", "typescript", "yaml", etc.
    description: str
    workspace_name: str  # directory name for source code at project_root/<workspace_name>/
    port: Optional[int] = None
    depends_on: List[str] = []
    requirements: List[str] = []  # pip/npm packages
    skeleton: Optional[str] = None  # fastapi | react | angular | teams-backend | teams-consumer | teams-frontend | none
    image_name: Optional[str] = None  # Docker image tag, set after build


class SystemArchitecture(BaseModel):
    """Full system architecture produced by the architect."""
    project_name: str
    project_slug: str  # e.g. "pet_groomer"
    services: List[ServiceDefinition]
    docker_compose: str  # generated docker-compose.yml content
    description: str


class ServiceResult(BaseModel):
    """Result of dispatching an engineer for one service."""
    service_name: str
    workspace_name: str
    success: bool
    issues_total: int = 0
    issues_passed: int = 0
    error: Optional[str] = None


class ArchitectResult(BaseModel):
    """Overall result of the architect pipeline."""
    project_name: str
    architecture: SystemArchitecture
    service_results: List[ServiceResult]
    docker_compose_path: Optional[str] = None
    project_root: Optional[str] = None


class AutoArchitectError(Exception):
    pass


class AutoArchitectBadAIResponseError(AutoArchitectError):
    pass
