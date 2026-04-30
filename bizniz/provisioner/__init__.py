"""
Provisioner — materializes a SystemArchitecture on disk.

The architect plans (services, dependencies, ports). The provisioner takes
that plan and produces the concrete project: directory tree, skeleton
seeding, infrastructure config files (postgres init.sql, FusionAuth
kickstart YAML, etc.), Dockerfiles for app services without skeletons,
docker-compose.yml, .env, and built Docker images.

Public API::

    from bizniz.provisioner import Provisioner

    provisioner = Provisioner(project_parent="/some/parent")
    result = provisioner.provision(architecture, project_name="Pet Groomer")
    # result.project_root, result.service_workspaces, result.compose_path, ...

Templates for common infrastructure services live in
``bizniz.provisioner.templates`` and are registered in
``templates.registry``.
"""
from bizniz.provisioner.provisioner import Provisioner
from bizniz.provisioner.types import (
    ProbedService,
    ProvisionedService,
    ProvisionResult,
    ProvisionState,
    ReconcileAction,
)

__all__ = [
    "Provisioner",
    "ProvisionResult",
    "ProvisionedService",
    "ProbedService",
    "ProvisionState",
    "ReconcileAction",
]
