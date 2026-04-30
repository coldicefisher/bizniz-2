"""Provisioner result types."""
from __future__ import annotations

from typing import Dict, List, Optional
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


class ProvisionerError(Exception):
    pass
