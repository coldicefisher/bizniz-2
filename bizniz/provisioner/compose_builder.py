"""Deterministic docker-compose.yml builder.

Replaces the AI-generated compose YAML with a structured assembly from
the SystemArchitecture + per-service template outputs. Idempotent and
testable.
"""
from __future__ import annotations

from typing import Dict, List

import yaml

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner.templates.base import TemplateOutput


_APP_SERVICE_TYPES = {"backend", "frontend", "worker"}


def build_compose(
    architecture: SystemArchitecture,
    template_outputs: Dict[str, TemplateOutput],
    project_slug: str,
) -> str:
    """Build the docker-compose.yml content for the project.

    Parameters
    ----------
    architecture:
        The plan from AutoArchitect — services + ports + dependencies.
    template_outputs:
        ``{service_name: TemplateOutput}`` for any service that had a
        template render (infrastructure templates plus app-template
        outputs for skeleton-less app services).
    project_slug:
        Project slug used for image tags ``<slug>-<service>:dev``.

    Returns
    -------
    YAML string ready to write to ``infra/development/docker-compose.yml``.
    """
    services_block: Dict[str, dict] = {}
    volumes: List[str] = []
    networks: List[str] = []

    for service in architecture.services:
        out = template_outputs.get(service.name)

        if out and out.compose_service is not None:
            # Template provided a complete service definition.
            services_block[service.name] = out.compose_service
            for v in out.compose_volumes:
                if v not in volumes:
                    volumes.append(v)
            for n in out.compose_networks:
                if n not in networks:
                    networks.append(n)
            continue

        # No template entry — must be an app service (skeleton-seeded or
        # generated). Build a standard app-service compose entry.
        if service.service_type in _APP_SERVICE_TYPES:
            entry = _build_app_service_entry(
                service, project_slug, architecture,
            )
            services_block[service.name] = entry
            if "app-network" not in networks:
                networks.append("app-network")

    # Top-level structure
    compose: Dict[str, object] = {"services": services_block}
    if volumes:
        compose["volumes"] = {v: None for v in volumes}
    if networks:
        compose["networks"] = {n: None for n in networks}

    return yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)


def _build_app_service_entry(
    service: ServiceDefinition,
    project_slug: str,
    architecture: SystemArchitecture,
) -> dict:
    """Compose entry for an application service (backend/frontend/worker)."""
    ws = service.workspace_name
    entry: dict = {
        "build": {
            "context": f"../../{ws}",
            "dockerfile": f"../../infra/development/{ws}/Dockerfile",
        },
        "env_file": ".env",
        "volumes": [f"../../{ws}:/app"],
        "networks": ["app-network"],
    }

    if service.port:
        # Container port comes from the service's framework defaults if not
        # explicit. For skeleton-seeded services the architect prompt should
        # have set service.port to the host side; container side derived
        # from skeleton metadata. Here we keep them equal as a reasonable
        # default; the architect can populate more nuanced mappings later.
        container_port = _container_port_for(service)
        entry["ports"] = [f"{service.port}:{container_port}"]

    # Resolve dependencies that exist in the architecture
    valid_deps = {s.name for s in architecture.services if s.name != service.name}
    deps = [d for d in service.depends_on if d in valid_deps]
    if deps:
        # If any dep is a database, use service_healthy condition where
        # postgres exposes a healthcheck.
        depends_block: dict = {}
        for d in deps:
            dep_svc = next((s for s in architecture.services if s.name == d), None)
            if dep_svc and dep_svc.service_type in {"database", "cache"}:
                depends_block[d] = {"condition": "service_healthy"}
            else:
                depends_block[d] = {"condition": "service_started"}
        entry["depends_on"] = depends_block

    return entry


def _container_port_for(service: ServiceDefinition) -> int:
    """Best guess for the in-container port a service exposes."""
    framework_ports = {
        "fastapi": 8000,
        "flask": 5000,
        "django": 8000,
        "react": 5173,
        "angular": 4200,
        "vue": 5173,
    }
    p = framework_ports.get(service.framework)
    if p:
        return p
    return service.port or 8000
