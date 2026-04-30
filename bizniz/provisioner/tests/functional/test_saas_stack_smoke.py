"""Heavy end-to-end smoke for the saas bundle.

Same shape as ``test_full_stack_smoke.py``, but the problem statement is
deliberately SaaS-shaped — real-time updates, long-running background
jobs, user profiles, OAuth login. The architect should pick the
``saas-*`` skeletons for this; the existing heavy smoke uses a plain
CRM prompt that picks ``fastapi`` + ``react`` because nothing in it
mentions realtime/jobs.

What this test asserts beyond the existing smoke:
  - The architect picks at least one of ``saas-api``, ``saas-ws``,
    ``saas-consumer``, ``saas-frontend`` (it might pick all four; we
    don't require that — the AI has discretion).
  - Postgres + FusionAuth + Redis are present (infra needed by the saas
    bundle).
  - The full stack comes up healthy and tears down cleanly.

Costs ~$0.20 in Gemini tokens + ~5 min wall-clock. Skipped without
GEMINI_API_KEY or a responsive Docker daemon.
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


SAAS_PROBLEM = (
    "Build a collaborative content platform. "
    "Users sign up and log in via OAuth (FusionAuth). "
    "Each user has a profile (display name, bio, avatar). "
    "Users author articles. Each article has a title and body. "
    "When a user clicks 'regenerate', the backend dispatches a "
    "long-running job (Redis Streams) that takes 5+ seconds; while the "
    "job runs, the article is locked and other users viewing it should "
    "see a real-time 'processing…' indicator pushed via WebSockets. "
    "When the job completes, viewers are notified live and the lock "
    "clears. "
    "Use Postgres for persistence, Redis for queue + pub/sub, FusionAuth "
    "for identity, FastAPI for the REST API, a dedicated WebSocket "
    "server, and an Angular SPA. "
    "Match the saas-* skeleton bundle conventions."
)


pytestmark = [
    pytest.mark.functional,
    pytest.mark.smoke,
    pytest.mark.timeout(900),
]


SAAS_SKELETONS = {"saas-api", "saas-ws", "saas-consumer", "saas-frontend"}


def _ensure_keys():
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set — skipping saas smoke test")


# ── Engineer stub ────────────────────────────────────────────────────────────


class _NoopEngineerCM:
    """Engineer factory stub — keeps Architect.build() happy without
    burning tokens on per-service codegen. Identical to the one in
    test_full_stack_smoke.py."""

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


def test_saas_stack_smoke(tmp_path):
    _ensure_keys()
    ensure_docker()

    project_name = f"Saas Smoke {int(time.time())}"
    log_dir = tmp_path / "_diagnostics"

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
        SAAS_PROBLEM,
        project_name=project_name,
        parallel=False,
        layered=False,
    )
    project_root = Path(result.project_root)
    compose_path = project_root / "infra" / "development" / "docker-compose.yml"
    assert compose_path.is_file(), f"docker-compose.yml not at {compose_path}"

    # ── Architect-selection assertions ────────────────────────────────────────
    chosen_skeletons = {
        s.skeleton for s in result.architecture.services
        if s.skeleton and s.skeleton != "none"
    }
    chosen_frameworks = {s.framework for s in result.architecture.services}
    chosen_types = {s.service_type for s in result.architecture.services}

    print(f"[saas-smoke] skeletons selected: {chosen_skeletons}")
    print(f"[saas-smoke] frameworks: {chosen_frameworks}")
    print(f"[saas-smoke] service types: {chosen_types}")

    # Required: at least one saas-* skeleton picked. The architect has
    # some discretion (it might pick saas-api alone, or the full quad),
    # but if NONE of the saas-* bundle was selected for a problem this
    # explicitly SaaS-shaped, the architect prompt isn't telling the AI
    # what saas-* is for.
    saas_chosen = chosen_skeletons & SAAS_SKELETONS
    assert saas_chosen, (
        f"Architect picked NO saas-* skeletons for an explicitly SaaS-shaped "
        f"problem. Got skeletons: {chosen_skeletons}, frameworks: "
        f"{chosen_frameworks}. The architect prompt may not surface the saas "
        f"bundle adequately."
    )

    # FusionAuth must be present — the prompt explicitly says OAuth + FA.
    assert "fusionauth" in chosen_frameworks, (
        f"Expected fusionauth framework. Got: {chosen_frameworks}"
    )

    # Postgres + Redis are required infra for the saas bundle.
    assert ("postgres" in chosen_frameworks
            or "database" in chosen_types), (
        f"Expected a postgres database. Got types: {chosen_types}, "
        f"frameworks: {chosen_frameworks}"
    )
    assert "redis" in chosen_frameworks, (
        f"Expected redis (the saas bundle needs Pub/Sub + Streams). "
        f"Got: {chosen_frameworks}"
    )

    failed_builds = [
        svc.name for svc in result.architecture.services
        if svc.service_type in ("backend", "frontend", "worker")
        and not image_present(f"{result.architecture.project_slug}-{svc.name}:dev")
    ]

    # ── Stack up + poll ────────────────────────────────────────────────────
    try:
        if failed_builds:
            pytest.fail(
                f"Image build failed for: {failed_builds}. "
                f"compose: {compose_path}"
            )

        up = compose(compose_path, "up", "-d", timeout=240)
        if up.returncode != 0:
            pytest.fail(
                f"`docker compose up -d` failed (rc={up.returncode}):\n"
                f"STDOUT:\n{up.stdout}\nSTDERR:\n{up.stderr}"
            )

        services_by_type: dict = {}
        for s in result.architecture.services:
            services_by_type.setdefault(s.service_type, []).append(s)

        # Backend(s): any HTTP response counts as "alive".
        for backend in services_by_type.get("backend", []):
            if not backend.port:
                continue
            assert http_alive(f"http://localhost:{backend.port}/", timeout=120), \
                f"Backend {backend.name} on :{backend.port} did not respond in 120s"

        # Frontend(s).
        for frontend in services_by_type.get("frontend", []):
            if not frontend.port:
                continue
            assert http_alive(f"http://localhost:{frontend.port}/", timeout=180), \
                f"Frontend {frontend.name} on :{frontend.port} did not respond in 180s"

        # Worker(s) like saas-ws have a /health endpoint; others (the
        # consumer) are headless. Poll the ones with ports.
        for worker in services_by_type.get("worker", []):
            if not worker.port:
                continue
            assert http_alive(f"http://localhost:{worker.port}/health", timeout=120), \
                f"Worker {worker.name} on :{worker.port}/health did not respond in 120s"

        # FusionAuth: kickstart is slow.
        for auth in services_by_type.get("auth", []):
            if not auth.port:
                continue
            assert http_alive(
                f"http://localhost:{auth.port}/api/status",
                timeout=300, expect_ok=True,
            ), f"FusionAuth {auth.name} on :{auth.port} didn't reach /api/status 200"

    except Exception:
        capture_diagnostics(compose_path, log_dir)
        print(f"[saas-smoke] diagnostics dumped to {log_dir}")
        raise

    finally:
        compose_down(compose_path)
