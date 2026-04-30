"""Tests for the postgres infrastructure template."""
from pathlib import Path

from bizniz.architect.types import ServiceDefinition
from bizniz.provisioner.templates import PostgresTemplate
from bizniz.provisioner.templates.base import TemplateContext


def _ctx(service: ServiceDefinition, slug: str = "myapp") -> TemplateContext:
    return TemplateContext(
        service=service, project_slug=slug, project_root=Path("/tmp"),
    )


def _service() -> ServiceDefinition:
    return ServiceDefinition(
        name="postgres",
        service_type="database",
        framework="postgres",
        language="sql",
        description="primary db",
        workspace_name="postgres",
        port=5433,
        depends_on=[],
        requirements=[],
        skeleton="none",
    )


def test_compose_entry_uses_alpine_image():
    out = PostgresTemplate().render(_ctx(_service()))
    assert out.compose_service is not None
    assert out.compose_service["image"] == "postgres:16-alpine"


def test_port_mapping_uses_service_host_port():
    svc = _service()
    svc.port = 15432
    out = PostgresTemplate().render(_ctx(svc))
    assert "15432:5432" in out.compose_service["ports"]


def test_default_port_when_none():
    svc = _service()
    svc.port = None
    out = PostgresTemplate().render(_ctx(svc))
    assert "5432:5432" in out.compose_service["ports"]


def test_healthcheck_uses_pg_isready():
    out = PostgresTemplate().render(_ctx(_service()))
    test_cmd = out.compose_service["healthcheck"]["test"]
    assert "pg_isready" in " ".join(test_cmd)


def test_init_sql_creates_fusionauth_database():
    out = PostgresTemplate().render(_ctx(_service()))
    init_sql = out.infra_files.get("postgres/init.sql")
    assert init_sql is not None
    assert "CREATE DATABASE fusionauth" in init_sql
    assert "GRANT ALL PRIVILEGES ON DATABASE fusionauth" in init_sql


def test_env_vars_include_database_url_with_project_slug():
    out = PostgresTemplate().render(_ctx(_service(), slug="petgroomer"))
    env = out.env_vars
    assert env["POSTGRES_USER"] == "dev"
    assert env["POSTGRES_DB"] == "petgroomer"
    assert "petgroomer" in env["DATABASE_URL"]


def test_compose_volumes_include_pgdata():
    out = PostgresTemplate().render(_ctx(_service()))
    assert "pgdata" in out.compose_volumes
