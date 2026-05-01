"""Unit tests for the post-build domain-coverage verifier.

Locks in the contract: a FastAPI backend whose ``/openapi.json``
shows only the skeleton's baseline (auth + health) gets downgraded
to ``success=False`` with a ``domain_dark`` error. This is the
regression bug we caught from run #3 — tests passed but the running
container served zero domain endpoints because the engineer's
routers were never mounted in app/main.py.

We don't run real Docker here; the verifier is decomposed so the
classification step can be exercised against a fake openapi document.
"""
from __future__ import annotations

from unittest.mock import patch

from bizniz.architect.smoke_verification import (
    _SKELETON_DEFAULT_PATHS,
    _backend_services,
    _mark_failed,
    verify_domain_coverage,
)
from bizniz.architect.types import (
    ServiceDefinition,
    ServiceResult,
    SystemArchitecture,
)


def _arch_with_backend(port: int = 8000) -> SystemArchitecture:
    return SystemArchitecture(
        project_name="x",
        project_slug="x",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="api", workspace_name="backend",
                port=port,
            ),
            ServiceDefinition(
                name="frontend", service_type="frontend", framework="react",
                language="typescript", description="ui", workspace_name="frontend",
                port=5173,
            ),
        ],
        description="x",
    )


def _ok_result(name: str) -> ServiceResult:
    return ServiceResult(
        service_name=name, workspace_name=name, success=True,
        issues_total=4, issues_passed=4,
    )


def test_backend_services_filters_to_fastapi_with_port():
    arch = _arch_with_backend()
    backends = _backend_services(arch)
    assert len(backends) == 1
    assert backends[0].name == "backend"


def test_dark_domain_marks_service_failed():
    """Backend exposes ONLY skeleton baseline routes — should fail."""
    arch = _arch_with_backend()
    initial = [_ok_result("backend"), _ok_result("frontend")]
    bare_skeleton_doc = {
        "paths": {p: {} for p in _SKELETON_DEFAULT_PATHS}
    }

    with patch(
        "bizniz.architect.smoke_verification._docker_available", return_value=True
    ), patch(
        "bizniz.architect.smoke_verification.subprocess.run"
    ) as mock_run, patch(
        "bizniz.architect.smoke_verification._wait_for_openapi",
        return_value=bare_skeleton_doc,
    ):
        # subprocess.run for both `compose up` and `compose down` must succeed
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        out = verify_domain_coverage(
            architecture=arch, service_results=initial,
            compose_path="/fake/compose.yml",
        )

    by_name = {r.service_name: r for r in out}
    assert by_name["backend"].success is False
    assert "domain_dark" in (by_name["backend"].error or "")
    # Frontend untouched
    assert by_name["frontend"].success is True


def test_domain_routes_present_keeps_service_passing():
    """Backend exposes domain routes past baseline — should pass."""
    arch = _arch_with_backend()
    initial = [_ok_result("backend")]
    domain_doc = {
        "paths": {
            "/health": {},
            "/api/v1/auth/login": {},
            "/api/v1/services": {},      # domain route
            "/api/v1/appointments": {},  # domain route
        }
    }

    with patch(
        "bizniz.architect.smoke_verification._docker_available", return_value=True
    ), patch(
        "bizniz.architect.smoke_verification.subprocess.run"
    ) as mock_run, patch(
        "bizniz.architect.smoke_verification._wait_for_openapi",
        return_value=domain_doc,
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        out = verify_domain_coverage(
            architecture=arch, service_results=initial,
            compose_path="/fake/compose.yml",
        )

    assert out[0].success is True


def test_unreachable_backend_marks_service_failed():
    """Backend that never responds on /openapi.json fails as smoke_unreachable."""
    arch = _arch_with_backend()
    initial = [_ok_result("backend")]

    with patch(
        "bizniz.architect.smoke_verification._docker_available", return_value=True
    ), patch(
        "bizniz.architect.smoke_verification.subprocess.run"
    ) as mock_run, patch(
        "bizniz.architect.smoke_verification._wait_for_openapi",
        return_value=None,
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        out = verify_domain_coverage(
            architecture=arch, service_results=initial,
            compose_path="/fake/compose.yml",
        )

    assert out[0].success is False
    assert "smoke_unreachable" in (out[0].error or "")


def test_skipped_when_docker_unavailable():
    """If docker daemon isn't reachable, leave results untouched."""
    arch = _arch_with_backend()
    initial = [_ok_result("backend")]

    with patch(
        "bizniz.architect.smoke_verification._docker_available", return_value=False
    ):
        out = verify_domain_coverage(
            architecture=arch, service_results=initial,
            compose_path="/fake/compose.yml",
        )

    assert out is initial  # unchanged reference passed through


def test_skipped_when_no_fastapi_backends():
    """Non-FastAPI projects (e.g. Express, Spring) skip verification."""
    arch = SystemArchitecture(
        project_name="x", project_slug="x",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend", framework="express",
                language="typescript", description="api", workspace_name="backend",
                port=8000,
            ),
        ],
        description="x",
    )
    initial = [_ok_result("backend")]

    out = verify_domain_coverage(
        architecture=arch, service_results=initial,
        compose_path="/fake/compose.yml",
    )
    assert out is initial


def test_compose_up_failure_does_not_corrupt_results():
    """If `docker compose up` fails, leave results untouched and tear down."""
    arch = _arch_with_backend()
    initial = [_ok_result("backend")]

    with patch(
        "bizniz.architect.smoke_verification._docker_available", return_value=True
    ), patch(
        "bizniz.architect.smoke_verification.subprocess.run"
    ) as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "compose: oh no"

        out = verify_domain_coverage(
            architecture=arch, service_results=initial,
            compose_path="/fake/compose.yml",
        )

    assert out[0].success is True  # untouched on infra failure
    # Verify compose down was attempted (cleanup)
    down_calls = [c for c in mock_run.call_args_list if "down" in str(c)]
    assert down_calls, "compose down should be attempted on up failure"


def test_mark_failed_replaces_in_place():
    a = _ok_result("a")
    b = _ok_result("b")
    results = [a, b]
    by_name = {r.service_name: r for r in results}

    _mark_failed(results, by_name, "a", "domain_dark: x")

    assert results[0].success is False
    assert "domain_dark" in (results[0].error or "")
    assert results[1].success is True  # b untouched
