"""
Auto Architect types.
"""

from typing import List, Optional, Dict
from pydantic import BaseModel


class ServiceDefinition(BaseModel):
    """A single service/container in the system architecture."""
    name: str
    service_type: str  # "backend", "frontend", "database", "cache", "proxy", etc.
    framework: str  # "fastapi", "angular", "nginx", "redis", "postgres", etc.
    language: str  # "python", "typescript", "yaml", etc.
    description: str
    workspace_name: str  # slug like "dog_breeder_backend"
    port: Optional[int] = None
    depends_on: List[str] = []


class SystemArchitecture(BaseModel):
    """Full system architecture produced by the architect."""
    project_name: str
    project_slug: str  # e.g. "dog_breeder"
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


class AutoArchitectError(Exception):
    pass


class AutoArchitectBadAIResponseError(AutoArchitectError):
    pass
