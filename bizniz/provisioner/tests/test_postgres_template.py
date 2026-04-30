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
    # Driver tag matters: SQLAlchemy's create_async_engine defaults to
    # psycopg2 without it, but skeletons install asyncpg, not psycopg2.
    assert env["DATABASE_URL"].startswith("postgresql+asyncpg://")


def test_database_url_uses_actual_service_name_not_hardcoded_postgres():
    """The architect can name the database service anything ("db", "data").
    The DATABASE_URL hostname must use the ACTUAL service name — hardcoding
    'postgres' causes DNS failures inside containers when the architect
    picks a different name. (Heavy AI smoke surfaced this 2026-04-30.)
    """
    db_svc = ServiceDefinition(
        name="db",  # ← architect's pick, not "postgres"
        service_type="database", framework="postgres", language="sql",
        description="primary db", workspace_name="postgres", port=5433,
        depends_on=[], requirements=[], skeleton="none",
    )
    out = PostgresTemplate().render(_ctx(db_svc, slug="myapp"))
    assert "@db:5432/" in out.env_vars["DATABASE_URL"], (
        f"DATABASE_URL hardcoded a hostname instead of using the service "
        f"name. Got: {out.env_vars['DATABASE_URL']}"
    )
    assert "@postgres:5432" not in out.env_vars["DATABASE_URL"]


def test_compose_volumes_include_pgdata():
    out = PostgresTemplate().render(_ctx(_service()))
    assert "pgdata" in out.compose_volumes
