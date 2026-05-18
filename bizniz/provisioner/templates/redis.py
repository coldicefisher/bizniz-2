"""Redis infrastructure template."""
from __future__ import annotations

from bizniz.provisioner.templates.base import (
    InfraTemplate,
    TemplateContext,
    TemplateOutput,
)


class RedisTemplate(InfraTemplate):
    """Standard redis deployment with health check. No config file needed
    for typical dev use."""

    DEFAULT_CONTAINER_PORT = 6379

    def render(self, ctx: TemplateContext) -> TemplateOutput:
        from bizniz.architect.types import host_port_for
        host_port = host_port_for(ctx.service) or 6379

        compose_service = {
            "image": "redis:7-alpine",
            "ports": [f"{host_port}:6379"],
            "healthcheck": {
                "test": ["CMD", "redis-cli", "ping"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 5,
            },
            "networks": ["app-network"],
        }

        return TemplateOutput(
            compose_service=compose_service,
            compose_networks=["app-network"],
            # Hostname is the actual service name — architect may pick
            # "redis", "cache", "queue", etc. Hardcoding "redis" causes
            # DNS failures inside containers when the architect picks a
            # different name. We emit BOTH the URL and the
            # host/port pair because skeletons differ in which env vars
            # they read.
            env_vars={
                "REDIS_URL": f"redis://{ctx.service.name}:6379/0",
                "REDIS_HOST": ctx.service.name,
                "REDIS_PORT": "6379",
            },
        )
