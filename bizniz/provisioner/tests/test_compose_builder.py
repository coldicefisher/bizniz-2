"""Tests for the deterministic docker-compose builder."""
import yaml

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner.compose_builder import build_compose
from bizniz.provisioner.templates.base import TemplateOutput


def _arch(*services) -> SystemArchitecture:
    return SystemArchitecture(
        project_name="X",
        project_slug="x",
        services=list(services),
        description="t",
    )


def _svc(name, type_, framework, language, port=None, depends_on=None, skeleton="none") -> ServiceDefinition:
    return ServiceDefinition(
        name=name, service_type=type_, framework=framework, language=language,
        description=name, workspace_name=name, port=port,
        depends_on=depends_on or [], requirements=[], skeleton=skeleton,
    )


def test_app_service_only_produces_build_entry():
    arch = _arch(_svc("backend", "backend", "fastapi", "python", port=8001, skeleton="fastapi"))
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    backend = parsed["services"]["backend"]
    assert backend["build"]["context"] == "../../backend"
    assert backend["build"]["dockerfile"] == "../../infra/development/backend/Dockerfile"
    assert "8001:8000" in backend["ports"]
    assert "../../backend:/app" in backend["volumes"]
    assert backend["env_file"] == ".env"
    # network registered
    assert "app-network" in parsed["networks"]


def test_template_provided_compose_used_when_present():
    arch = _arch(_svc("redis", "cache", "redis", "yaml", port=6380))
    out = TemplateOutput(
        compose_service={
            "image": "redis:7-alpine",
            "ports": ["6380:6379"],
        },
        compose_networks=["app-network"],
    )
    yml = build_compose(arch, template_outputs={"redis": out}, project_slug="x")
    parsed = yaml.safe_load(yml)
    assert parsed["services"]["redis"]["image"] == "redis:7-alpine"
    assert "6380:6379" in parsed["services"]["redis"]["ports"]


def test_volumes_aggregated_from_template_outputs():
    arch = _arch(_svc("postgres", "database", "postgres", "sql", port=5433))
    out = TemplateOutput(
        compose_service={"image": "postgres:16-alpine"},
        compose_volumes=["pgdata"],
    )
    yml = build_compose(arch, template_outputs={"postgres": out}, project_slug="x")
    parsed = yaml.safe_load(yml)
    assert "pgdata" in parsed["volumes"]


def test_dependency_uses_service_healthy_for_db():
    arch = _arch(
        _svc("postgres", "database", "postgres", "sql", port=5433),
        _svc("backend", "backend", "fastapi", "python", port=8001, depends_on=["postgres"], skeleton="fastapi"),
    )
    pg_out = TemplateOutput(
        compose_service={"image": "postgres:16-alpine"},
    )
    yml = build_compose(arch, template_outputs={"postgres": pg_out}, project_slug="x")
    parsed = yaml.safe_load(yml)
    backend_deps = parsed["services"]["backend"]["depends_on"]
    assert backend_deps["postgres"]["condition"] == "service_healthy"


def test_dependency_uses_service_started_for_app():
    arch = _arch(
        _svc("backend", "backend", "fastapi", "python", port=8001, skeleton="fastapi"),
        _svc("frontend", "frontend", "react", "typescript", port=5173, depends_on=["backend"], skeleton="react"),
    )
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    frontend_deps = parsed["services"]["frontend"]["depends_on"]
    assert frontend_deps["backend"]["condition"] == "service_started"


def test_unknown_dependency_is_dropped():
    arch = _arch(
        _svc("backend", "backend", "fastapi", "python", port=8001, depends_on=["ghost"], skeleton="fastapi"),
    )
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    assert "depends_on" not in parsed["services"]["backend"]


def test_react_uses_5173_container_port():
    arch = _arch(_svc("frontend", "frontend", "react", "typescript", port=5174, skeleton="react"))
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    assert "5174:5173" in parsed["services"]["frontend"]["ports"]


def test_angular_uses_4200_container_port():
    arch = _arch(_svc("dashboard", "frontend", "angular", "typescript", port=4201, skeleton="angular"))
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    assert "4201:4200" in parsed["services"]["dashboard"]["ports"]
