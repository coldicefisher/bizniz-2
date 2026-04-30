"""Heavy end-to-end smoke test.

Runs the FULL pipeline against a real AI provider:
  1. Architect.decompose against Gemini for a CRM-shaped problem.
  2. Provisioner.provision with build_images=True — actual `docker build`.
  3. `docker compose up -d` to bring the stack up.
  4. Poll each service's HTTP endpoint and verify it responds.
  5. Tear down everything (compose down -v --rmi all).

This test deliberately costs money and time. It exists to catch the
silent failures unit tests can't see: Dockerfiles that don't build,
compose files that won't parse, services that crash on startup,
network/depends_on misconfigurations.

For a faster, free version that uses a hand-crafted architecture,
see test_full_stack_smoke_no_ai.py.

Skipped automatically when:
  - GEMINI_API_KEY isn't set
  - docker isn't on PATH or the daemon isn't responsive

Run explicitly with::

    pytest -m "functional and smoke" bizniz/provisioner/tests/functional/

Engineer dispatch is stubbed — we test the infrastructure path only.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from bizniz.architect.architect import Architect
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.provisioner import Provisioner
from bizniz.provisioner.tests.functional._smoke_helpers import (
    capture_diagnostics,
    compose,
    compose_down,
    ensure_docker,
    http_alive,
    image_present,
)
from bizniz.workspace.local_workspace import LocalWorkspace


CRM_PROBLEM = (
    "Build a small CRM web application. "
    "Customers can sign up and log in (OAuth). "
    "Authenticated users can manage contacts (CRUD), companies (CRUD), and "
    "deals attached to a contact. "
    "Backend exposes a REST API. Frontend is a single-page app. "
    "Use a relational database for persistence."
)


pytestmark = [
    pytest.mark.functional,
    pytest.mark.smoke,
    pytest.mark.timeout(900),
]


def _ensure_keys():
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set — skipping smoke test")


# ── Engineer stub ────────────────────────────────────────────────────────────


class _NoopEngineerCM:
    """Engineer factory stub — keeps Architect.build() happy without
    spending tokens on per-service codegen."""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def analyze(self, problem_statement):
        from bizniz.engineer.types import EngineeringAnalysis
        return EngineeringAnalysis(
            problem_id=0, requirements=[], use_cases=[], issues=[],
        )

    def run_three_phase(self, problem_statement, analysis=None):
        return []


# ── The test ─────────────────────────────────────────────────────────────────


def test_full_stack_smoke(tmp_path):
    _ensure_keys()
    ensure_docker()

    project_name = f"Smoke CRM {int(time.time())}"
    log_dir = tmp_path / "_diagnostics"

    # 1. Plan + provision (builds images)
    config = BiznizConfig.find_and_load()
    architect_client = config.make_client(model=config.architect_model)
    workspace = LocalWorkspace(root=tmp_path / "_arch_workspace")
    provisioner = Provisioner(
        project_parent=tmp_path,
        build_images=True,
        on_status_message=lambda m: print(f"[provisioner] {m}"),
    )
    architect = Architect(
        client=architect_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        engineer_factory=lambda *a, **kw: _NoopEngineerCM(),
        project_parent=str(tmp_path),
        provisioner=provisioner,
        on_status_message=lambda m: print(f"[architect] {m}"),
    )

    result = architect.build(
        CRM_PROBLEM,
        project_name=project_name,
        parallel=False,
        layered=False,
    )
    project_root = Path(result.project_root)
    compose_path = project_root / "infra" / "development" / "docker-compose.yml"
    assert compose_path.is_file(), f"docker-compose.yml not created at {compose_path}"

    failed_builds = [
        svc.name for svc in result.architecture.services
        if svc.service_type in ("backend", "frontend", "worker")
        and not image_present(f"{result.architecture.project_slug}-{svc.name}:dev")
    ]

    # 2. Bring stack up + poll endpoints (in try/finally for guaranteed teardown).
    try:
        if failed_builds:
            pytest.fail(
                f"Image build failed for: {failed_builds}. "
                f"docker-compose.yml: {compose_path}"
            )

        up = compose(compose_path, "up", "-d", timeout=180)
        if up.returncode != 0:
            pytest.fail(
                f"`docker compose up -d` failed (rc={up.returncode}):\n"
                f"STDOUT:\n{up.stdout}\nSTDERR:\n{up.stderr}"
            )

        services_by_type = {s.service_type: s for s in result.architecture.services}
        backend = services_by_type.get("backend")
        frontend = services_by_type.get("frontend")
        auth = services_by_type.get("auth")

        if backend and backend.port:
            assert http_alive(
                f"http://localhost:{backend.port}/", timeout=120,
            ), (
                f"Backend on http://localhost:{backend.port}/ did not "
                f"respond within 120s — image likely starts then crashes"
            )

        if frontend and frontend.port:
            assert http_alive(
                f"http://localhost:{frontend.port}/", timeout=120,
            ), (
                f"Frontend on http://localhost:{frontend.port}/ did not "
                f"respond within 120s"
            )

        if auth and auth.port:
            assert http_alive(
                f"http://localhost:{auth.port}/api/status",
                timeout=300, expect_ok=True,
            ), (
                f"FusionAuth on http://localhost:{auth.port} did not "
                f"reach /api/status 200 within 300s"
            )

    except Exception:
        capture_diagnostics(compose_path, log_dir)
        print(f"[smoke] diagnostics dumped to {log_dir}")
        raise

    finally:
        compose_down(compose_path)
