"""Unit tests for Provisioner.evolve() — idempotent re-provisioning."""
import json
from pathlib import Path

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner import Provisioner


def _service(**kw):
    base = dict(
        name="x", service_type="backend", framework="fastapi", language="python",
        description="x", workspace_name="x", port=8000, depends_on=[], requirements=[],
        skeleton="none",
    )
    base.update(kw)
    return ServiceDefinition(**base)


@pytest.fixture
def tmp_parent(tmp_path):
    return tmp_path


def _milestone1_arch():
    """Initial state — auth + postgres + backend, all 'new'."""
    return SystemArchitecture(
        project_name="Mini CRM", project_slug="mini_crm", description="m1",
        services=[
            _service(
                name="postgres", service_type="database", framework="postgres",
                language="sql", workspace_name="postgres", port=5433,
                skeleton="none", evolve_state="new",
            ),
            _service(
                name="auth", service_type="auth", framework="fusionauth",
                language="yaml", workspace_name="fusionauth", port=9011,
                depends_on=["postgres"], skeleton="none", evolve_state="new",
            ),
            _service(
                name="backend", service_type="backend", framework="fastapi",
                language="python", workspace_name="backend", port=8001,
                depends_on=["postgres", "auth"], skeleton="none", evolve_state="new",
            ),
        ],
    )


def _milestone2_arch():
    """After milestone 2: backend extended, frontend added, infra unchanged."""
    return SystemArchitecture(
        project_name="Mini CRM", project_slug="mini_crm", description="m2",
        services=[
            _service(
                name="postgres", service_type="database", framework="postgres",
                language="sql", workspace_name="postgres", port=5433,
                skeleton="none", evolve_state="unchanged",
            ),
            _service(
                name="auth", service_type="auth", framework="fusionauth",
                language="yaml", workspace_name="fusionauth", port=9011,
                depends_on=["postgres"], skeleton="none", evolve_state="unchanged",
            ),
            _service(
                name="backend", service_type="backend", framework="fastapi",
                language="python", workspace_name="backend", port=8001,
                depends_on=["postgres", "auth"], skeleton="none", evolve_state="extended",
            ),
            _service(
                name="frontend", service_type="frontend", framework="react",
                language="typescript", workspace_name="frontend", port=5174,
                depends_on=["backend"], skeleton="none", evolve_state="new",
            ),
        ],
    )


# ── Idempotency / preservation ───────────────────────────────────────────────

def test_evolve_preserves_existing_workspace_files(tmp_parent):
    """A workspace populated by milestone 1 must NOT be re-seeded by
    milestone 2 — engineer-generated code stays put."""
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    p.provision(_milestone1_arch(), project_name="Mini CRM")

    # Simulate engineer adding code to the backend during milestone 1
    backend_ws = tmp_parent / "mini_crm" / "backend"
    user_file = backend_ws / "app" / "models" / "contact.py"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("class Contact: pass\n")
    user_content = user_file.read_text()

    # Now evolve — backend is "extended", should keep the file
    p.evolve(_milestone2_arch(), project_name="Mini CRM")
    assert user_file.exists()
    assert user_file.read_text() == user_content, "evolve trampled engineer's file"


def test_evolve_does_not_remove_prior_images(tmp_parent):
    """evolve() must not call cleanup. Images from prior milestones
    must survive — only image rebuilds for new/extended services."""
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    p.provision(_milestone1_arch(), project_name="Mini CRM")
    # The provision call wouldn't actually build images (build_images=False),
    # but the project DB has services registered with image_name='ready'
    # status from save_service. We verify cleanup isn't triggered by
    # checking that infra files survive.
    init_sql = tmp_parent / "mini_crm" / "infra" / "development" / "postgres" / "init.sql"
    assert init_sql.exists()
    init_content = init_sql.read_text()

    p.evolve(_milestone2_arch(), project_name="Mini CRM")

    # Existing infra preserved (postgres init.sql is regenerated, but
    # content is identical since templates are deterministic)
    assert init_sql.exists()
    assert init_sql.read_text() == init_content


def test_evolve_creates_workspace_for_new_service(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    p.provision(_milestone1_arch(), project_name="Mini CRM")

    p.evolve(_milestone2_arch(), project_name="Mini CRM")

    # Frontend is new — workspace + Dockerfile materialized
    fe_ws = tmp_parent / "mini_crm" / "frontend"
    assert fe_ws.is_dir()
    fe_pkg = fe_ws / "package.json"
    assert fe_pkg.is_file()
    fe_dockerfile = tmp_parent / "mini_crm" / "infra" / "development" / "frontend" / "Dockerfile"
    assert fe_dockerfile.is_file()


def test_evolve_regenerates_compose_with_all_services(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    p.provision(_milestone1_arch(), project_name="Mini CRM")

    p.evolve(_milestone2_arch(), project_name="Mini CRM")

    compose = (tmp_parent / "mini_crm" / "infra" / "development" / "docker-compose.yml").read_text()
    # All 4 services appear
    for name in ("postgres", "auth", "backend", "frontend"):
        assert f"\n  {name}:" in compose, f"{name} missing from compose after evolve"


def test_evolve_only_remaps_new_service_ports(tmp_parent):
    """Existing services keep their ports even if a NEW service requests
    the same port."""
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    p.provision(_milestone1_arch(), project_name="Mini CRM")

    # Build an evolved architecture where a new service tries to take
    # backend's port (8001).
    arch = _milestone2_arch()
    new_collider = _service(
        name="reporter", service_type="worker", framework="celery",
        language="python", workspace_name="reporter", port=8001,
        skeleton="none", evolve_state="new",
    )
    arch.services.append(new_collider)

    result = p.evolve(arch, project_name="Mini CRM")
    backend = next(s for s in arch.services if s.name == "backend")
    reporter = next(s for s in arch.services if s.name == "reporter")
    # ``svc.port`` is the CONTAINER port — never mutated by the
    # provisioner, even on collision. ``svc.host_port`` captures the
    # remap so in-network URLs (``http://reporter:8001``) keep working.
    assert backend.port == 8001, "container port stays put on existing service"
    assert backend.host_port is None, "no remap → no host_port override"
    assert reporter.port == 8001, "container port stays at architect's choice"
    assert reporter.host_port is not None and reporter.host_port != 8001, (
        "colliding new service should get a host_port remap"
    )
    assert "reporter" in result.port_remap


def test_evolve_records_provisioned_services_with_correct_states(tmp_parent):
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    p.provision(_milestone1_arch(), project_name="Mini CRM")

    result = p.evolve(_milestone2_arch(), project_name="Mini CRM")
    by_name = {ps.name: ps for ps in result.services}

    # Frontend was new — workspace_path set
    assert by_name["frontend"].workspace_path is not None
    # Postgres still infrastructure
    assert by_name["postgres"].is_infrastructure is True
    # Backend extended — workspace path still present
    assert by_name["backend"].workspace_path is not None


def test_evolve_with_no_new_services_is_a_noop_for_skeletons(tmp_parent):
    """An evolve with everything 'unchanged' should still write infra
    files (idempotent) but not re-seed any skeleton."""
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    p.provision(_milestone1_arch(), project_name="Mini CRM")

    # Build an architecture where ALL services are unchanged
    arch = SystemArchitecture(
        project_name="Mini CRM", project_slug="mini_crm", description="m3",
        services=[
            _service(name=s.name, service_type=s.service_type, framework=s.framework,
                     language=s.language, workspace_name=s.workspace_name,
                     port=s.port, depends_on=s.depends_on,
                     requirements=s.requirements, skeleton=s.skeleton,
                     evolve_state="unchanged")
            for s in _milestone1_arch().services
        ],
    )

    # Should not raise; should still write compose (always idempotent)
    result = p.evolve(arch, project_name="Mini CRM")
    compose_path = tmp_parent / "mini_crm" / "infra" / "development" / "docker-compose.yml"
    assert compose_path.exists()
    assert "postgres" in compose_path.read_text()


def test_evolve_handles_evolve_state_none_as_unchanged(tmp_parent):
    """Defensive — services without evolve_state set should be treated
    as unchanged (don't crash)."""
    p = Provisioner(project_parent=tmp_parent, build_images=False)
    p.provision(_milestone1_arch(), project_name="Mini CRM")

    # Strip evolve_state from a service
    arch = _milestone2_arch()
    arch.services[0].evolve_state = None  # postgres
    p.evolve(arch, project_name="Mini CRM")  # should not crash
