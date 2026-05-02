"""Base classes and registry for provisioner templates."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture

# Common aliases for framework names the AI may return in
# non-canonical form (e.g. "PostgreSQL" instead of "postgres").
_ALIASES = {
    "postgresql": "postgres",
    "redis-server": "redis",
}


@dataclass
class TemplateContext:
    """Per-service context passed to a template.

    Templates that cross-reference siblings (e.g. fusionauth needs to
    know which service is the postgres database) read from
    ``architecture.services``. The architect can name services anything
    — never assume "postgres" is literally a service name.
    """
    service: ServiceDefinition
    project_slug: str
    project_root: Path
    architecture: Optional[SystemArchitecture] = None
    # Map of host_port -> container_port for any port mappings (after
    # free-port allocation).
    port_mappings: List[tuple] = field(default_factory=list)

    def find_by_framework(self, framework: str) -> Optional[ServiceDefinition]:
        """Find a sibling service by framework (case-insensitive). Returns None if absent."""
        if self.architecture is None:
            return None
        target = framework.lower()
        target = _ALIASES.get(target, target)
        for s in self.architecture.services:
            key = s.framework.lower()
            key = _ALIASES.get(key, key)
            if key == target and s.name != self.service.name:
                return s
        return None


@dataclass
class TemplateOutput:
    """What a template emits for one service.

    The provisioner gathers these across all services and writes them to
    disk in one pass.

    Fields:
      - compose_service: dict for the service's entry under ``services:``
        in docker-compose.yml. ``None`` skips compose registration (rare).
      - compose_volumes: top-level volume names this service needs
        (e.g. ``["pgdata"]``).
      - compose_networks: top-level network names
      - workspace_files: {relative_path: content} written under
        ``project_root/<workspace_name>/``
      - infra_files: {relative_path: content} written under
        ``project_root/infra/development/<workspace_name>/``
      - env_vars: {KEY: value} merged into ``.env``
      - depends_on_services: extra services this template wants present
        (e.g. fusionauth needs postgres). The provisioner enforces this.
    """
    compose_service: Optional[dict] = None
    compose_volumes: List[str] = field(default_factory=list)
    compose_networks: List[str] = field(default_factory=list)
    workspace_files: Dict[str, str] = field(default_factory=dict)
    infra_files: Dict[str, str] = field(default_factory=dict)
    env_vars: Dict[str, str] = field(default_factory=dict)
    depends_on_services: List[str] = field(default_factory=list)


class InfraTemplate(ABC):
    """Base class for an infrastructure-service template."""

    name: str = ""

    @abstractmethod
    def render(self, ctx: TemplateContext) -> TemplateOutput:
        """Produce the files + compose entry + env vars for this service."""
        raise NotImplementedError


# ── Registry ──────────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, InfraTemplate] = {}


def register(name: str, template: InfraTemplate) -> None:
    """Register a template under a lookup key (framework name or sentinel)."""
    template.name = name
    _REGISTRY[name] = template


def lookup(framework: str) -> Optional[InfraTemplate]:
    """Find a template by framework name (case-insensitive + aliases).

    The architect AI may return "FusionAuth" or "PostgreSQL" while
    templates register as "fusionauth" and "postgres". Normalizing
    to lowercase + aliasing prevents silent misses.
    """
    key = framework.lower()
    key = _ALIASES.get(key, key)
    return _REGISTRY.get(key)


def all_templates() -> Dict[str, InfraTemplate]:
    return dict(_REGISTRY)
