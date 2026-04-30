"""End-to-end smoke test that doesn't call the AI.

Hand-crafts a representative SystemArchitecture (postgres + fusionauth +
fastapi backend + react frontend) and feeds it directly to the Provisioner.
The Provisioner does the work that's actually likely to break:
skeleton clone, image build, compose generation, network/depends_on
wiring, kickstart + init.sql provisioning.

Then `docker compose up -d` brings the stack up; we poll each port and
verify it responds; we always tear down.

This test is heavy (Docker, ~2-3 min) but free (no AI tokens). Suitable
for gating merges.

Skipped when docker isn't available. Run explicitly with::

    pytest -m "functional and smoke" bizniz/provisioner/tests/functional/test_full_stack_smoke_no_ai.py
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner import Provisioner
from bizniz.provisioner.tests.functional._smoke_helpers import (
    capture_diagnostics,
    compose,
    compose_down,
    ensure_docker,
    http_alive,
    image_present,
)


pytestmark = [
    pytest.mark.functional,
    pytest.mark.smoke,
    pytest.mark.timeout(600),
]


def _crm_architecture(slug: str, project_name: str) -> SystemArchitecture:
    """The same shape Architect typically emits for the CRM_PROBLEM:
    postgres, fusionauth, fastapi backend (skeleton-seeded), react
    frontend (skeleton-seeded). Hand-crafted so we don't pay for AI."""
    return SystemArchitecture(
        project_name=project_name,
        project_slug=slug,
        description="Smoke-test CRM: hand-crafted architecture, no AI involved.",
        services=[
            ServiceDefinition(
                name="postgres",
                service_type="database",
                framework="postgres",
                language="sql",
                description="Primary relational store",
                workspace_name="postgres",
                port=5433,
                depends_on=[],
                requirements=[],
                skeleton="none",
                evolve_state="new",
            ),
            ServiceDefinition(
                name="auth",
                service_type="auth",
                framework="fusionauth",
                language="yaml",
                description="OAuth provider via FusionAuth",
                workspace_name="fusionauth",
                port=9013,
                depends_on=["postgres"],
                requirements=[],
                skeleton="none",
                evolve_state="new",
            ),
            ServiceDefinition(
                name="backend",
                service_type="backend",
                framework="fastapi",
                language="python",
                description="REST API for the CRM",
                workspace_name="backend",
                port=8003,
                depends_on=["postgres", "auth"],
                requirements=[],
                skeleton="fastapi",
                evolve_state="new",
            ),
            ServiceDefinition(
                name="frontend",
                service_type="frontend",
                framework="react",
                language="typescript",
                description="Single-page CRM UI",
                workspace_name="frontend",
                port=5176,
                depends_on=["backend"],
                requirements=[],
                skeleton="react",
                evolve_state="new",
            ),
        ],
    )


def test_full_stack_smoke_no_ai(tmp_path):
    ensure_docker()

    # Unique slug per run so reruns don't trip on prior project state.
    slug = f"smoke_noai_{int(time.time())}"
    project_name = slug.replace("_", " ").title()
    architecture = _crm_architecture(slug=slug, project_name=project_name)

    log_dir = tmp_path / "_diagnostics"

    provisioner = Provisioner(
        project_parent=tmp_path,
        build_images=True,
        on_status_message=lambda m: print(f"[provisioner] {m}"),
    )

    # 1. Materialize: skeleton clone (cached), template render, docker build.
    result = provisioner.provision(architecture, project_name=project_name)
    project_root = Path(result.project_root)
    compose_path = Path(result.compose_path)
    assert compose_path.is_file(), f"docker-compose.yml missing at {compose_path}"

    # Sanity: the skeleton-derived files landed where compose expects.
    assert (project_root / "backend" / "Dockerfile").is_file(), \
        "backend skeleton Dockerfile missing — auto-clone or seed failed"
    assert (project_root / "frontend" / "Dockerfile").is_file(), \
        "frontend skeleton Dockerfile missing"
    assert (project_root / "infra/development/postgres/init.sql").is_file(), \
        "postgres init.sql missing — FusionAuth template didn't run"
    assert (project_root / "infra/development/fusionauth/kickstart/kickstart.json").is_file(), \
        "fusionauth kickstart.json missing"

    # Verify images built.
    failed_builds = [
        svc.name for svc in architecture.services
        if svc.service_type in ("backend", "frontend", "worker")
        and not image_present(f"{slug}-{svc.name}:dev")
    ]

    # 2. Bring stack up + poll endpoints.
    try:
        if failed_builds:
            pytest.fail(
                f"Image build failed for: {failed_builds}. "
                f"docker-compose.yml: {compose_path}"
            )

        print(f"[smoke] docker compose up -d (project={slug})...")
        t0 = time.time()
        up = compose(compose_path, "up", "-d", timeout=180)
        if up.returncode != 0:
            pytest.fail(
                f"`docker compose up -d` failed (rc={up.returncode}):\n"
                f"STDOUT:\n{up.stdout}\nSTDERR:\n{up.stderr}"
            )
        print(f"[smoke] compose up succeeded in {time.time() - t0:.1f}s")

        services_by_type = {s.service_type: s for s in architecture.services}
        backend = services_by_type.get("backend")
        frontend = services_by_type.get("frontend")
        auth = services_by_type.get("auth")

        # Backend: any HTTP response = the server is reachable.
        print(f"[smoke] polling backend http://localhost:{backend.port}/ ...")
        t0 = time.time()
        backend_alive = http_alive(
            f"http://localhost:{backend.port}/", timeout=120,
        )
        print(f"[smoke] backend alive={backend_alive} in {time.time() - t0:.1f}s")
        assert backend_alive, \
            f"Backend on http://localhost:{backend.port}/ did not respond within 120s"

        # Frontend: any HTTP response.
        print(f"[smoke] polling frontend http://localhost:{frontend.port}/ ...")
        t0 = time.time()
        frontend_alive = http_alive(
            f"http://localhost:{frontend.port}/", timeout=120,
        )
        print(f"[smoke] frontend alive={frontend_alive} in {time.time() - t0:.1f}s")
        assert frontend_alive, \
            f"Frontend on http://localhost:{frontend.port}/ did not respond within 120s"

        # FusionAuth: must reach 200 on /api/status (slow boot, kickstart).
        print(f"[smoke] polling fusionauth http://localhost:{auth.port}/api/status (expect 200, may take 60-180s) ...")
        t0 = time.time()
        auth_ok = http_alive(
            f"http://localhost:{auth.port}/api/status",
            timeout=300, expect_ok=True,
        )
        print(f"[smoke] fusionauth ok={auth_ok} in {time.time() - t0:.1f}s")
        assert auth_ok, \
            f"FusionAuth on http://localhost:{auth.port} did not reach /api/status 200 within 300s"

    except Exception:
        capture_diagnostics(compose_path, log_dir)
        print(f"[smoke-no-ai] diagnostics dumped to {log_dir}")
        raise

    finally:
        compose_down(compose_path)
