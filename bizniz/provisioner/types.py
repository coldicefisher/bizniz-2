"""Provisioner result types."""
from __future__ import annotations

from typing import Dict, List, Literal, Optional
from pydantic import BaseModel


class ProvisionedService(BaseModel):
    """A service after the provisioner has materialized it on disk."""
    name: str
    workspace_name: str
    workspace_path: Optional[str] = None  # None for infrastructure services
    image_name: Optional[str] = None  # Tagged Docker image (when built)
    image_built: bool = False
    is_infrastructure: bool = False
    template_name: Optional[str] = None  # postgres, redis, fusionauth, ...
    skeleton_name: Optional[str] = None  # fastapi, react, angular, ...


class ProvisionResult(BaseModel):
    """Result of provisioning an entire SystemArchitecture."""
    project_name: str
    project_slug: str
    project_root: str
    compose_path: str
    env_path: str
    services: List[ProvisionedService] = []
    port_remap: Dict[str, tuple] = {}  # service_name -> (old_port, new_port)


class ProbedService(BaseModel):
    """Observed state of a single service — what's actually on disk / in DB."""
    name: str
    db_recorded: bool = False
    db_workspace_path: Optional[str] = None
    db_status: Optional[str] = None
    db_image_name: Optional[str] = None
    workspace_exists_on_disk: bool = False
    has_dockerfile: bool = False
    image_in_docker: bool = False


class ProvisionState(BaseModel):
    """Snapshot of an existing project's state, used by the reconciler."""
    project_slug: str
    project_root: str
    project_root_exists: bool
    compose_exists: bool = False
    env_exists: bool = False
    last_architecture_snapshot_json: Optional[str] = None
    services: List[ProbedService] = []
    orphan_workspace_dirs: List[str] = []  # FS workspaces without a DB row
    project_images: List[str] = []         # All <slug>-*:dev images in docker

    def get_service(self, name: str) -> Optional[ProbedService]:
        for s in self.services:
            if s.name == name:
                return s
        return None


class ReconcileAction(BaseModel):
    """Per-service action plan emitted by the reconciler."""
    service_name: str
    action: Literal["create", "update", "preserve"]
    rebuild_image: bool = False
    reason: str = ""


class ProvisionerError(Exception):
    pass
