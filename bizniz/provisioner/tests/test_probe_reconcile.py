"""Unit tests for Provisioner.probe() + _reconcile().

Covers state observation (DB rows, FS workspaces, Docker images, orphans)
and the per-service action plan that flows out of reconcile.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner import Provisioner, ProvisionState
from bizniz.provisioner.types import ProbedService, ReconcileAction


def _service(**kw) -> ServiceDefinition:
    base = dict(
        name="x", service_type="backend", framework="fastapi", language="python",
        description="x", workspace_name="x", port=8000,
        depends_on=[], requirements=[], skeleton="none",
    )
    base.update(kw)
    return ServiceDefinition(**base)


def _arch(*services, slug="proj", project_name="Proj") -> SystemArchitecture:
    return SystemArchitecture(
        project_name=project_name, project_slug=slug, description="d",
        services=list(services),
    )


# ── probe() ─────────────────────────────────────────────────────────────────

def test_probe_on_missing_project_returns_empty_state(tmp_path):
    p = Provisioner(project_parent=tmp_path, build_images=False)
    state = p.probe("does_not_exist", tmp_path / "does_not_exist")
    assert state.project_root_exists is False
    assert state.services == []
    assert state.orphan_workspace_dirs == []
    assert state.project_images == []
    assert state.last_architecture_snapshot_json is None


def test_probe_after_fresh_provision_sees_all_services(tmp_path):
    """provision() runs probe internally first (empty state), then materializes.
    Probing again should now see everything."""
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="postgres", service_type="database", framework="postgres",
                 language="sql", workspace_name="postgres", port=5433, skeleton="none"),
        _service(name="backend", service_type="backend", framework="fastapi",
                 language="python", workspace_name="backend", port=8001, skeleton="none"),
    )
    p.provision(arch, project_name="Proj")

    with patch.object(p, "_list_project_images", return_value=[]):
        state = p.probe("proj")

    assert state.project_root_exists
    assert state.compose_exists
    assert state.env_exists
    by_name = {s.name: s for s in state.services}
    assert "postgres" in by_name
    assert "backend" in by_name
    assert by_name["backend"].db_recorded
    assert by_name["backend"].workspace_exists_on_disk
    assert by_name["backend"].has_dockerfile
    assert state.last_architecture_snapshot_json is not None


def test_probe_detects_orphan_workspace_dirs(tmp_path):
    """A directory under project_root that isn't tracked in DB shows up as orphan."""
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none"),
    )
    p.provision(arch, project_name="Proj")

    # Plant an extra workspace dir not in DB.
    (tmp_path / "proj" / "deprecated_service").mkdir()
    (tmp_path / "proj" / "deprecated_service" / "main.py").write_text("# old")

    with patch.object(p, "_list_project_images", return_value=[]):
        state = p.probe("proj")

    assert "deprecated_service" in state.orphan_workspace_dirs
    # And the legitimate service is not flagged as orphan
    assert "backend" not in state.orphan_workspace_dirs


def test_probe_detects_db_recorded_but_workspace_missing(tmp_path):
    """Drift case: DB has a service row but the workspace dir is gone."""
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none"),
    )
    p.provision(arch, project_name="Proj")

    # Nuke the backend workspace.
    import shutil
    shutil.rmtree(tmp_path / "proj" / "backend")

    with patch.object(p, "_list_project_images", return_value=[]):
        state = p.probe("proj")

    backend = state.get_service("backend")
    assert backend is not None
    assert backend.db_recorded is True
    assert backend.workspace_exists_on_disk is False


def test_probe_detects_image_presence(tmp_path):
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none"),
    )
    p.provision(arch, project_name="Proj")

    with patch.object(
        p, "_list_project_images", return_value=["proj-backend:dev", "proj-extra:dev"],
    ):
        state = p.probe("proj")
    assert state.project_images == ["proj-backend:dev", "proj-extra:dev"]
    backend = state.get_service("backend")
    assert backend.image_in_docker is True


# ── _reconcile() ────────────────────────────────────────────────────────────

def _action_for(actions, name) -> ReconcileAction:
    return next(a for a in actions if a.service_name == name)


def test_reconcile_first_build_marks_all_create(tmp_path):
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="postgres", service_type="database", framework="postgres",
                 workspace_name="postgres", port=5433, skeleton="none"),
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none"),
    )
    empty_state = ProvisionState(
        project_slug="proj", project_root=str(tmp_path / "proj"),
        project_root_exists=False,
    )
    actions = p._reconcile(arch, empty_state)
    assert _action_for(actions, "postgres").action == "create"
    assert _action_for(actions, "backend").action == "create"
    assert all(a.rebuild_image for a in actions if a.service_name == "backend")


def test_reconcile_extended_service_gets_update(tmp_path):
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none", evolve_state="extended"),
    )
    state = ProvisionState(
        project_slug="proj", project_root=str(tmp_path / "proj"),
        project_root_exists=True,
        services=[ProbedService(
            name="backend", db_recorded=True,
            workspace_exists_on_disk=True, has_dockerfile=True,
            image_in_docker=True,
        )],
    )
    actions = p._reconcile(arch, state)
    a = _action_for(actions, "backend")
    assert a.action == "update"
    assert a.rebuild_image is True


def test_reconcile_unchanged_service_gets_preserve(tmp_path):
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none", evolve_state="unchanged"),
    )
    state = ProvisionState(
        project_slug="proj", project_root=str(tmp_path / "proj"),
        project_root_exists=True,
        services=[ProbedService(
            name="backend", db_recorded=True,
            workspace_exists_on_disk=True, has_dockerfile=True,
            image_in_docker=True,
        )],
    )
    actions = p._reconcile(arch, state)
    a = _action_for(actions, "backend")
    assert a.action == "preserve"
    assert a.rebuild_image is False


def test_reconcile_unchanged_but_image_missing_triggers_rebuild(tmp_path):
    """If a service is otherwise unchanged but its docker image is gone
    (e.g. user pruned it), reconcile should still flag rebuild_image."""
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none", evolve_state="unchanged"),
    )
    state = ProvisionState(
        project_slug="proj", project_root=str(tmp_path / "proj"),
        project_root_exists=True,
        services=[ProbedService(
            name="backend", db_recorded=True,
            workspace_exists_on_disk=True, has_dockerfile=True,
            image_in_docker=False,  # image gone
        )],
    )
    actions = p._reconcile(arch, state)
    a = _action_for(actions, "backend")
    assert a.action == "preserve"
    assert a.rebuild_image is True
    assert "image missing" in a.reason


def test_reconcile_db_recorded_but_workspace_missing_triggers_create(tmp_path):
    """Drift recovery: DB says service exists, FS disagrees → rebuild from scratch."""
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none", evolve_state="unchanged"),
    )
    state = ProvisionState(
        project_slug="proj", project_root=str(tmp_path / "proj"),
        project_root_exists=True,
        services=[ProbedService(
            name="backend", db_recorded=True,
            workspace_exists_on_disk=False,  # workspace gone
            has_dockerfile=False,
            image_in_docker=False,
        )],
    )
    actions = p._reconcile(arch, state)
    a = _action_for(actions, "backend")
    assert a.action == "create"
    assert "workspace missing" in a.reason


def test_reconcile_infrastructure_does_not_need_workspace_drift_check(tmp_path):
    """Infrastructure services have no workspace on disk; reconcile must not
    flag them as drifted just because workspace_exists_on_disk is False."""
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="postgres", service_type="database", framework="postgres",
                 workspace_name="postgres", port=5433, skeleton="none",
                 evolve_state="unchanged"),
    )
    state = ProvisionState(
        project_slug="proj", project_root=str(tmp_path / "proj"),
        project_root_exists=True,
        services=[ProbedService(
            name="postgres", db_recorded=True,
            workspace_exists_on_disk=False,  # infra has no workspace
            has_dockerfile=False,
            image_in_docker=False,
        )],
    )
    actions = p._reconcile(arch, state)
    a = _action_for(actions, "postgres")
    assert a.action == "preserve"


# ── End-to-end: re-running provision is idempotent ──────────────────────────

def test_re_provisioning_same_arch_does_not_duplicate_or_rebuild(tmp_path):
    """Calling provision() twice with the same arch should be a no-op for
    materialization (no re-seeding, no port remapping). Images would be
    rebuilt only if missing — disabled here via build_images=False."""
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none"),
    )
    r1 = p.provision(arch, project_name="Proj")
    # Mutate a workspace file the user might have edited
    (Path(r1.project_root) / "backend" / "user_added.py").write_text("user code")

    r2 = p.provision(arch, project_name="Proj")

    # User's file survives
    assert (Path(r2.project_root) / "backend" / "user_added.py").read_text() == "user code"
    # Compose still in place
    assert Path(r2.compose_path).exists()
    # No port remap on second run — there was nothing to collide with
    assert r2.port_remap == {}


def test_provision_with_prune_calls_orphan_image_cleanup(tmp_path):
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none"),
    )
    with patch.object(p, "_prune_orphan_images") as mock_prune, \
         patch.object(p, "_list_project_images", return_value=[]):
        p.provision(arch, project_name="Proj", prune=True)
    mock_prune.assert_called_once()


def test_provision_without_prune_skips_orphan_cleanup(tmp_path):
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none"),
    )
    with patch.object(p, "_prune_orphan_images") as mock_prune, \
         patch.object(p, "_list_project_images", return_value=[]):
        p.provision(arch, project_name="Proj", prune=False)
    mock_prune.assert_not_called()


def test_evolve_is_alias_for_provision_no_prune(tmp_path):
    p = Provisioner(project_parent=tmp_path, build_images=False)
    arch = _arch(
        _service(name="backend", service_type="backend", workspace_name="backend",
                 port=8001, skeleton="none"),
    )
    with patch.object(p, "_prune_orphan_images") as mock_prune, \
         patch.object(p, "_list_project_images", return_value=[]):
        p.evolve(arch, project_name="Proj")
    mock_prune.assert_not_called()
