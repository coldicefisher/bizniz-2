"""Post-build domain-coverage smoke verification.

After ``architect.build()`` finishes and every service's tests pass, we
still don't know whether the running stack actually exposes the
domain the user asked for. The classic failure mode caught by this:
the FastAPI engineer wrote a parallel ``pet_groomer/`` package with
correct router code, all tests passed, container came up — but the
deployed app served only the skeleton's auth router because the
generated routers were never mounted in ``app/main.py``. Tests
imported the routers directly and were happy.

This verifier runs ``docker compose up -d`` against the just-built
project, polls each backend's ``/openapi.json``, and asserts that at
least one non-default route exists past the skeleton's own auth +
health surface. If the route surface looks like the bare skeleton,
the service is downgraded to ``success=False`` with a specific
``domain_dark`` error.

Failure here means the build "succeeded" by tests but the deployed
stack doesn't actually do what was asked. We'd rather fail the run
than ship that to a human reviewer.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from typing import Callable, List, Optional, Set

from bizniz.architect.types import ServiceDefinition, ServiceResult, SystemArchitecture


# Paths the FastAPI skeleton ships out of the box. Any service exposing
# only paths in this set has a "dark" domain — the engineer never wired
# its generated routers into the entrypoint.
_SKELETON_DEFAULT_PATHS: Set[str] = {
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/me",
    "/api/v1/auth/verify-email",
    "/api/v1/auth/resend-verification",
    "/api/v1/auth/forgot-password",
    "/api/v1/auth/reset-password",
    "/api/v1/auth/oauth/google",
}


def _log(on_status: Optional[Callable[[str], None]], msg: str) -> None:
    if on_status:
        on_status(msg)


def _docker_available() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=5,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _http_get_json(url: str, timeout: float = 5.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _wait_for_openapi(port: int, deadline_s: float) -> Optional[dict]:
    """Poll ``/openapi.json`` on localhost:<port> until it returns or we
    give up. Returns the parsed openapi document or None."""
    end = time.monotonic() + deadline_s
    url = f"http://localhost:{port}/openapi.json"
    while time.monotonic() < end:
        doc = _http_get_json(url, timeout=3.0)
        if doc is not None and isinstance(doc.get("paths"), dict):
            return doc
        time.sleep(2.0)
    return None


def _backend_services(arch: SystemArchitecture) -> List[ServiceDefinition]:
    return [
        s for s in arch.services
        if s.service_type == "backend" and s.framework == "fastapi" and s.port
    ]


def verify_domain_coverage(
    architecture: SystemArchitecture,
    service_results: List[ServiceResult],
    compose_path: str,
    on_status: Optional[Callable[[str], None]] = None,
    backend_wait_s: float = 60.0,
) -> List[ServiceResult]:
    """Run ``docker compose up -d``, verify each FastAPI backend exposes
    domain routes past the skeleton baseline, then bring the stack
    down. Returns a possibly-mutated copy of ``service_results`` with
    ``success=False`` set on any backend whose domain is dark.

    Best-effort: if Docker isn't available, returns the input unchanged
    with a status log. We don't fail builds for verifier infra issues.
    """
    backends = _backend_services(architecture)
    if not backends:
        _log(on_status, "Smoke verify: no FastAPI backends to verify, skipping")
        return service_results

    if not _docker_available():
        _log(on_status, "Smoke verify: docker unavailable, skipping")
        return service_results

    _log(on_status, f"Smoke verify: bringing up stack ({len(backends)} backend(s))...")

    up = subprocess.run(
        ["docker", "compose", "-f", compose_path, "up", "-d"],
        capture_output=True, text=True, timeout=240,
    )
    if up.returncode != 0:
        _log(
            on_status,
            f"Smoke verify: compose up failed (rc={up.returncode}); skipping verification. "
            f"stderr: {up.stderr.strip()[:300]}"
        )
        try:
            subprocess.run(
                ["docker", "compose", "-f", compose_path, "down"],
                capture_output=True, text=True, timeout=120,
            )
        except Exception:
            pass
        return service_results

    results_by_name = {r.service_name: r for r in service_results}
    out: List[ServiceResult] = list(service_results)

    try:
        for backend in backends:
            doc = _wait_for_openapi(backend.port, deadline_s=backend_wait_s)
            if doc is None:
                _log(
                    on_status,
                    f"Smoke verify: '{backend.name}' did not expose /openapi.json on "
                    f":{backend.port} within {backend_wait_s:.0f}s — domain coverage unknown"
                )
                _mark_failed(
                    out, results_by_name, backend.name,
                    "smoke_unreachable: backend did not respond on /openapi.json",
                )
                continue

            paths = set(doc.get("paths", {}).keys())
            domain_paths = sorted(paths - _SKELETON_DEFAULT_PATHS)
            if not domain_paths:
                _log(
                    on_status,
                    f"Smoke verify: '{backend.name}' DOMAIN DARK — only baseline routes "
                    f"({sorted(paths & _SKELETON_DEFAULT_PATHS)[:3]}…) mounted. "
                    f"Generated routers were not wired into the skeleton entrypoint."
                )
                _mark_failed(
                    out, results_by_name, backend.name,
                    "domain_dark: backend exposes only the skeleton's baseline "
                    "routes; the engineer's generated routers were not mounted in "
                    "app/main.py. See SKELETON.md.",
                )
            else:
                _log(
                    on_status,
                    f"Smoke verify: '{backend.name}' OK — {len(domain_paths)} domain "
                    f"route(s): {domain_paths[:3]}{'…' if len(domain_paths) > 3 else ''}"
                )
    finally:
        _log(on_status, "Smoke verify: tearing down stack...")
        try:
            subprocess.run(
                ["docker", "compose", "-f", compose_path, "down"],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            _log(on_status, f"Smoke verify: teardown error ({e})")

    return out


def _mark_failed(
    results: List[ServiceResult],
    by_name: dict,
    name: str,
    error: str,
) -> None:
    """Replace ``results[i]`` for service ``name`` with a failed copy."""
    existing = by_name.get(name)
    if existing is None:
        return
    failed = ServiceResult(
        service_name=existing.service_name,
        workspace_name=existing.workspace_name,
        success=False,
        issues_total=existing.issues_total,
        issues_passed=existing.issues_passed,
        error=error,
    )
    for i, r in enumerate(results):
        if r.service_name == name:
            results[i] = failed
            return
