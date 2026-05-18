"""Integration tests for the Provisioner end-to-end (no Docker).

Runs ``Provisioner.provision()`` with ``build_images=False`` against a
hand-crafted architecture and asserts the on-disk layout.
"""
import json
import tempfile
from pathlib import Path

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner import Provisioner


def _service(**overrides) -> ServiceDefinition:
    base = dict(
        name="x", service_type="backend", framework="fastapi", language="python",
        description="x", workspace_name="x", port=8000,
        depends_on=[], requirements=[], skeleton="none",
    )
    base.update(overrides)
    return ServiceDefinition(**base)


def _crm_arch() -> SystemArchitecture:
    """Mimic what the architect would emit for a CRM-like project."""
    return SystemArchitecture(
        project_name="Mini CRM",
        project_slug="mini_crm",
        description="CRM with auth",
        services=[
            _service(
                name="postgres", service_type="database", framework="postgres",
                language="sql", workspace_name="postgres", port=5433, skeleton="none",
            ),
            _service(
                name="auth", service_type="auth", framework="fusionauth",
                language="yaml", workspace_name="fusionauth", port=9012,
                depends_on=["postgres"], skeleton="none",
            ),
            _service(
                name="backend", service_type="backend", framework="fastapi",
                language="python", workspace_name="backend", port=8002,
                depends_on=["postgres", "auth"], skeleton="none",
            ),
            _service(
                name="frontend", service_type="frontend", framework="react",
                language="typescript", workspace_name="frontend", port=5174,
                depends_on=["backend"], skeleton="none",
            ),
        ],
    )


@pytest.fixture
def tmp_parent(tmp_path):
    return tmp_path


def test_provision_creates_project_root(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(_crm_arch(), project_name="Mini CRM")
    assert Path(result.project_root).is_dir()
    assert Path(result.project_root).name == "mini_crm"


def test_provision_writes_compose_yaml(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(_crm_arch(), project_name="Mini CRM")
    compose = Path(result.compose_path).read_text()
    # Every service appears
    for name in ("postgres", "auth", "backend", "frontend"):
        assert f"\n  {name}:" in compose, f"{name} missing from compose"
    # Backend depends on postgres healthy
    assert "service_healthy" in compose


def test_provision_writes_env_with_template_vars(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(_crm_arch(), project_name="Mini CRM")
    env = Path(result.env_path).read_text()
    assert "PROJECT_NAME=mini_crm" in env
    assert "POSTGRES_USER=" in env
    assert "FUSIONAUTH_API_KEY=" in env
    assert "FUSIONAUTH_APPLICATION_ID=" in env


def test_provision_writes_postgres_init_sql(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(_crm_arch(), project_name="Mini CRM")
    init = Path(result.project_root) / "infra/development/postgres/init.sql"
    assert init.is_file()
    assert "CREATE DATABASE fusionauth" in init.read_text()


def test_provision_writes_fusionauth_kickstart(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(_crm_arch(), project_name="Mini CRM")
    kick = Path(result.project_root) / "infra/development/fusionauth/kickstart/kickstart.json"
    assert kick.is_file()
    parsed = json.loads(kick.read_text())
    assert "requests" in parsed
    assert any("/api/application/" in r["url"] for r in parsed["requests"])


def test_provision_creates_app_service_workspaces(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(_crm_arch(), project_name="Mini CRM")
    backend_ws = Path(result.project_root) / "backend"
    frontend_ws = Path(result.project_root) / "frontend"
    assert backend_ws.is_dir()
    assert frontend_ws.is_dir()


def test_provision_python_template_writes_dockerfile_and_requirements(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(_crm_arch(), project_name="Mini CRM")
    backend_dockerfile = Path(result.project_root) / "infra/development/backend/Dockerfile"
    backend_reqs = Path(result.project_root) / "backend/requirements.txt"
    assert backend_dockerfile.is_file()
    assert backend_reqs.is_file()
    assert "FROM python:3.12-slim" in backend_dockerfile.read_text()
    assert "fastapi" in backend_reqs.read_text()


def test_provision_typescript_template_writes_package_json(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(_crm_arch(), project_name="Mini CRM")
    pkg = Path(result.project_root) / "frontend/package.json"
    assert pkg.is_file()
    parsed = json.loads(pkg.read_text())
    assert parsed["name"] == "mini_crm-frontend"
    assert "ts-jest" in parsed["devDependencies"]


def test_provision_records_provisioned_services(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(_crm_arch(), project_name="Mini CRM")
    by_name = {s.name: s for s in result.services}
    assert by_name["postgres"].is_infrastructure is True
    assert by_name["postgres"].template_name == "postgres"
    assert by_name["auth"].template_name == "fusionauth"
    assert by_name["backend"].is_infrastructure is False
    assert by_name["backend"].workspace_path is not None


def test_provision_skeleton_seeds_workspace(tmp_parent):
    arch = SystemArchitecture(
        project_name="X", project_slug="x", description="x",
        services=[
            _service(name="backend", service_type="backend", framework="fastapi",
                     language="python", workspace_name="backend", port=8000,
                     skeleton="fastapi"),
        ],
    )
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(arch, project_name="X")
    backend = Path(result.project_root) / "backend"
    # FastAPI skeleton has app/ directory and pytest tests
    assert (backend / "app").is_dir() or (backend / "Dockerfile").is_file()


def test_provision_port_remap_is_recorded(tmp_parent):
    """Two services requesting the same host port — second should be remapped."""
    arch = SystemArchitecture(
        project_name="X", project_slug="x", description="x",
        services=[
            _service(name="a", workspace_name="a", port=58000, skeleton="none"),
            _service(name="b", workspace_name="b", port=58000, skeleton="none"),
        ],
    )
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(arch, project_name="X")
    # Either a or b stays at 58000; the other moves
    assert "b" in result.port_remap or "a" in result.port_remap


def test_provision_remap_uses_host_port_field_not_mutation(tmp_parent, monkeypatch):
    """Regression: the provisioner must not mutate ``svc.port`` on
    collision. Container port (``svc.port``) stays at the architect's
    choice; the host-side remap lives on ``svc.host_port``. In-network
    URLs (``http://<svc>:<port>``) depend on this — pre-fix, the
    integration tester was building ``http://backend:8002`` (host port)
    inside a Docker network sidecar and getting Connection Refused."""
    from bizniz.architect.types import host_port_for
    # Isolate the cross-process port reservation registry so live
    # builds on the same machine don't bleed into the test's choices.
    monkeypatch.setenv("BIZNIZ_PROJECTS_ROOT", str(tmp_parent))
    arch = SystemArchitecture(
        project_name="X", project_slug="x_host_port", description="x",
        services=[
            _service(name="alpha", workspace_name="alpha", port=58000, skeleton="none"),
            _service(name="beta",  workspace_name="beta",  port=58000, skeleton="none"),
        ],
    )
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(arch, project_name="X")
    # Both still report port=58000 (container port — never mutated).
    assert arch.services[0].port == 58000
    assert arch.services[1].port == 58000
    # Exactly one of them got remapped via host_port.
    remapped = [s for s in arch.services if s.host_port is not None]
    not_remapped = [s for s in arch.services if s.host_port is None]
    assert len(remapped) == 1 and len(not_remapped) == 1, (
        f"expected 1 remapped + 1 unchanged; got remapped={remapped}"
    )
    # host_port_for() returns the right value in both cases.
    for s in arch.services:
        if s.host_port is not None:
            assert host_port_for(s) == s.host_port
            assert host_port_for(s) != 58000
        else:
            assert host_port_for(s) == 58000  # falls back to container port
    # Result's remap dict matches the host_port choice.
    assert remapped[0].name in result.port_remap


def test_provision_unknown_infra_framework_logs_no_template(tmp_parent):
    """When a service points at infrastructure with no registered template,
    we don't crash — just skip its compose entry and log."""
    arch = SystemArchitecture(
        project_name="X", project_slug="x", description="x",
        services=[
            _service(name="oddball", service_type="auth",
                     framework="some-other-iam", language="yaml",
                     workspace_name="oddball", port=9100, skeleton="none"),
        ],
    )
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    result = p.provision(arch, project_name="X")
    by_name = {s.name: s for s in result.services}
    assert by_name["oddball"].template_name is None
    assert by_name["oddball"].is_infrastructure is True
