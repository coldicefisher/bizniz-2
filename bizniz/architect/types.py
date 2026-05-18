"""
Auto Architect types.
"""

from typing import List, Optional
from pydantic import BaseModel, field_validator


class ServiceDefinition(BaseModel):
    """A single service/container in the system architecture."""
    name: str
    service_type: str  # "backend", "frontend", "database", "cache", "proxy", "auth", etc.
    framework: str  # "fastapi", "react", "angular", "nginx", "redis", "postgres", etc.
    language: str  # "python", "typescript", "yaml", etc.

    @field_validator("service_type", "framework", "language", mode="before")
    @classmethod
    def _normalize_lowercase(cls, v):
        """AI returns 'TypeScript', 'FastAPI', 'PostgreSQL' — normalize
        to lowercase so downstream comparisons (test environment selection,
        template lookup, skeleton matching) don't silently mismatch."""
        return v.lower() if isinstance(v, str) else v
    description: str
    workspace_name: str  # directory name for source code at project_root/<workspace_name>/
    # CONTAINER port — the port the service listens on inside its container.
    # Stable across remaps; safe to use in Docker-network internal URLs
    # (e.g. ``http://backend:{port}`` from a sidecar joined to the same
    # network). Architect sets this; Provisioner does NOT mutate it.
    port: Optional[int] = None
    # HOST port — the host-side port mapped to ``port``. None means
    # "same as container port" (the default when there's no conflict).
    # Provisioner sets this only when port-collision detection forces
    # a remap. Use for host-side URLs (browser, smoke tests, debugger
    # inspecting from the host). NEVER use for Docker-network URLs —
    # those always use ``port`` (the container side).
    host_port: Optional[int] = None
    depends_on: List[str] = []
    requirements: List[str] = []  # pip/npm packages
    skeleton: Optional[str] = None  # fastapi | react | angular | teams-backend | teams-consumer | teams-frontend | none
    image_name: Optional[str] = None  # Docker image tag, set after build
    # Evolve-mode tag set by Architect.evolve():
    #   "new"       — service didn't exist before this milestone
    #   "extended"  — service existed but this milestone adds work to it
    #   "unchanged" — service exists and this milestone doesn't touch it
    # On a fresh decompose() (no prior architecture), every service is "new".
    evolve_state: Optional[str] = None


class SystemArchitecture(BaseModel):
    """Full system architecture produced by the architect."""
    project_name: str
    project_slug: str  # e.g. "pet_groomer"
    services: List[ServiceDefinition]
    description: str
    # Optional AI-suggested compose preview retained for the human-readable
    # architecture doc only. The Provisioner builds the actual
    # docker-compose.yml deterministically from `services`.
    docker_compose: Optional[str] = None


class ServiceResult(BaseModel):
    """Result of dispatching an engineer for one service."""
    service_name: str
    workspace_name: str
    success: bool
    issues_total: int = 0
    issues_passed: int = 0
    error: Optional[str] = None


class ArchitectResult(BaseModel):
    """Overall result of the architect pipeline.

    ``success`` is the authoritative outcome for the milestone. It's
    True only if engineering ran AND all dispatched services passed.
    A milestone that aborted before engineering (e.g. FusionAuth
    contract couldn't be repaired, layer gate failed) reports
    success=False even when service_results is empty — empty
    service_results is not a vacuous pass.
    """
    project_name: str
    architecture: SystemArchitecture
    service_results: List[ServiceResult]
    docker_compose_path: Optional[str] = None
    project_root: Optional[str] = None
    success: bool = False
    abort_reason: Optional[str] = None


class ArchitectError(Exception):
    pass


class ArchitectBadAIResponseError(ArchitectError):
    pass


def host_port_for(svc: ServiceDefinition) -> Optional[int]:
    """Return the host-side port for a service: ``svc.host_port`` if the
    provisioner remapped, else ``svc.port`` (= container port, which by
    default is also exposed on the same host port). Use this for any
    host-side URL or compose ports declaration."""
    return svc.host_port if svc.host_port is not None else svc.port
