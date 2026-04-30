"""Tests for the redis infrastructure template."""
from pathlib import Path

from bizniz.architect.types import ServiceDefinition
from bizniz.provisioner.templates import RedisTemplate
from bizniz.provisioner.templates.base import TemplateContext


def _ctx(svc) -> TemplateContext:
    return TemplateContext(service=svc, project_slug="myapp", project_root=Path("/tmp"))


def _service(port: int | None = 6380) -> ServiceDefinition:
    return ServiceDefinition(
        name="redis",
        service_type="cache",
        framework="redis",
        language="yaml",
        description="cache",
        workspace_name="redis",
        port=port,
        depends_on=[],
        requirements=[],
        skeleton="none",
    )


def test_compose_uses_alpine_image():
    out = RedisTemplate().render(_ctx(_service()))
    assert out.compose_service["image"] == "redis:7-alpine"


def test_port_mapping():
    out = RedisTemplate().render(_ctx(_service(port=16379)))
    assert "16379:6379" in out.compose_service["ports"]


def test_healthcheck_pings():
    out = RedisTemplate().render(_ctx(_service()))
    assert "redis-cli" in " ".join(out.compose_service["healthcheck"]["test"])


def test_redis_url_env_var():
    out = RedisTemplate().render(_ctx(_service()))
    assert out.env_vars["REDIS_URL"] == "redis://redis:6379/0"


def test_redis_url_uses_actual_service_name():
    """If the architect names the service "cache" or "queue", REDIS_URL
    must use that hostname — hardcoding "redis" causes DNS failures
    inside containers."""
    from bizniz.architect.types import ServiceDefinition
    cache = ServiceDefinition(
        name="cache",  # architect's pick
        service_type="cache", framework="redis", language="yaml",
        description="cache + queue", workspace_name="cache", port=6379,
        depends_on=[], requirements=[], skeleton="none",
    )
    out = RedisTemplate().render(_ctx(cache))
    assert out.env_vars["REDIS_URL"] == "redis://cache:6379/0"


def test_no_workspace_or_infra_files():
    out = RedisTemplate().render(_ctx(_service()))
    assert out.workspace_files == {}
    assert out.infra_files == {}
