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
        host_port = ctx.service.port or 6379

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
            env_vars={"REDIS_URL": "redis://redis:6379/0"},
        )
